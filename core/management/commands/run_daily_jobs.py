"""The nightly batch: invoices, late fees, reminders, expiries, recurring expenses,
and clearing WhatsApp log entries past their 30 days."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounting.models import RecurringExpense
from billing.models import Invoice
from billing.services import apply_late_fees, generate_due_invoices
from core.models import Tenant
from guests.models import GuestDocument, Tenancy
from messaging import services as messaging
from messaging.models import ReminderRule

ALERT_BUCKETS = [30, 15, 7]


class Command(BaseCommand):
    help = 'Generate invoices, apply late fees and send the scheduled reminders.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', help='Slug of a single client to process.')
        parser.add_argument('--date', help='Run as if today were YYYY-MM-DD.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        today = timezone.localdate()
        if options.get('date'):
            today = timezone.datetime.strptime(options['date'], '%Y-%m-%d').date()

        tenants = Tenant.objects.filter(status=Tenant.STATUS_ACTIVE)
        if options.get('tenant'):
            tenants = tenants.filter(slug=options['tenant'])

        for tenant in tenants:
            self.stdout.write(self.style.MIGRATE_HEADING(f'— {tenant.name}'))
            invoices = generate_due_invoices(tenant, on_date=today)
            self.stdout.write(f'  invoices raised: {len(invoices)}')

            fees = apply_late_fees(tenant, on_date=today)
            self.stdout.write(f'  late fees updated: {len(fees)}')

            generated = [r.generate(today) for r in
                         RecurringExpense.objects.filter(tenant=tenant, is_active=True)]
            self.stdout.write(f'  recurring expenses: {len([g for g in generated if g])}')

            if options['dry_run']:
                continue

            sent = self.send_rent_reminders(tenant, today)
            self.stdout.write(f'  rent reminders: {sent}')
            expiring = self.send_document_alerts(tenant, today)
            self.stdout.write(f'  document alerts: {expiring}')

            purged = messaging.purge_old_messages(tenant)
            self.stdout.write(f'  old WhatsApp log entries deleted: {purged}')

    def send_rent_reminders(self, tenant, today):
        count = 0
        rules = ReminderRule.objects.filter(tenant=tenant, is_active=True).select_related('template')
        open_invoices = (
            Invoice.objects.filter(tenant=tenant)
            .exclude(status__counts_as_settled=True)
            .select_related('tenancy__guest', 'tenancy__building')
        )
        for rule in rules:
            target_due = today - timedelta(days=rule.offset_days)
            for invoice in open_invoices.filter(due_date=target_due):
                if invoice.balance <= 0:
                    continue
                messaging.send(
                    tenant=tenant,
                    template_code=rule.template.code,
                    to_number=invoice.tenancy.guest.messaging_number(),
                    context=messaging.base_context(
                        tenant, guest=invoice.tenancy.guest, tenancy=invoice.tenancy,
                        invoice=invoice,
                    ),
                    guest=invoice.tenancy.guest, tenancy=invoice.tenancy, invoice=invoice,
                )
                count += 1
        return count

    def send_document_alerts(self, tenant, today):
        count = 0
        for bucket in ALERT_BUCKETS:
            target = today + timedelta(days=bucket)
            rows = GuestDocument.objects.filter(
                tenant=tenant, expiry_date=target
            ).select_related('guest')
            for doc in rows:
                if doc.last_alert_day_bucket is not None and doc.last_alert_day_bucket <= bucket:
                    continue
                tenancy = doc.guest.current_tenancy()
                messaging.send(
                    tenant=tenant,
                    template_code='document_expiry',
                    to_number=doc.guest.messaging_number(),
                    context=messaging.base_context(
                        tenant, guest=doc.guest, tenancy=tenancy,
                        extra={'document': doc.get_doc_type_display(),
                               'expiry': doc.expiry_date.strftime('%d-%m-%Y'),
                               'days': str(bucket)},
                    ),
                    guest=doc.guest, tenancy=tenancy,
                )
                doc.last_alert_day_bucket = bucket
                doc.save(update_fields=['last_alert_day_bucket'])
                count += 1
        return count
