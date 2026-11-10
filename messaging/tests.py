"""Template rendering, number normalisation and the delivery log."""
from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from core.tests import ZyDwellTestCase
from guests import services as guest_services
from guests.models import GuestDocument
from messaging import services
from messaging.models import MessageLog, MessageTemplate


class NumberTests(ZyDwellTestCase):
    def test_local_numbers_become_international(self):
        self.assertEqual(services.normalise_number('0501234567'), '971501234567')
        self.assertEqual(services.normalise_number('+971 50 123 4567'), '971501234567')
        self.assertEqual(services.normalise_number('00971501234567'), '971501234567')
        self.assertEqual(services.normalise_number(''), '')


class TemplateTests(ZyDwellTestCase):
    def test_placeholders_are_filled(self):
        template = MessageTemplate.objects.get(tenant=self.tenant, code='rent_reminder')
        body = template.render({'guest': 'Rahul', 'currency': 'AED', 'amount': '800',
                                'bed': '101-A', 'due_date': '01-03-2026',
                                'building': 'Test Tower'})
        self.assertIn('Rahul', body)
        self.assertIn('AED 800', body)
        self.assertNotIn('{guest}', body)


class SendingTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'),
        )
        from billing.services import build_invoice

        self.invoice = build_invoice(
            self.tenancy, period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29),
            due_date=timezone.localdate(), user=self.owner)

    def full_context(self):
        return services.base_context(self.tenant, guest=self.guest,
                                     tenancy=self.tenancy, invoice=self.invoice)

    def test_an_unapproved_template_is_skipped_not_sent(self):
        entry = services.send(
            tenant=self.tenant, template_code='rent_reminder',
            to_number=self.guest.mobile,
            context=self.full_context(), guest=self.guest, tenancy=self.tenancy,
        )
        self.assertEqual(entry.status, MessageLog.STATUS_SKIPPED)
        self.assertIn('approved', entry.error)

    def test_an_approved_template_is_queued_while_sending_is_off(self):
        MessageTemplate.objects.filter(tenant=self.tenant).update(is_approved=True)
        entry = services.send(
            tenant=self.tenant, template_code='rent_reminder',
            to_number=self.guest.mobile,
            context=self.full_context(), guest=self.guest, tenancy=self.tenancy,
        )
        self.assertEqual(entry.status, MessageLog.STATUS_QUEUED)
        self.assertIn('Test Tower', entry.body)

    def test_a_guest_with_no_number_fails_and_is_logged(self):
        self.guest.mobile = ''
        self.guest.save()
        entry = services.send(
            tenant=self.tenant, template_code='rent_reminder', to_number='',
            context={}, guest=self.guest,
        )
        self.assertEqual(entry.status, MessageLog.STATUS_FAILED)

    def test_every_attempt_is_logged_for_proof(self):
        services.send(tenant=self.tenant, template_code='rent_reminder',
                      to_number=self.guest.mobile, context={}, guest=self.guest)
        self.assertEqual(MessageLog.objects.filter(tenant=self.tenant).count(), 1)


class DailyJobTests(ZyDwellTestCase):
    def test_the_nightly_job_raises_invoices_and_queues_reminders(self):
        from django.core.management import call_command
        from io import StringIO

        from billing.models import Invoice

        MessageTemplate.objects.filter(tenant=self.tenant).update(is_approved=True)
        guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=40),
            monthly_rent=Decimal('800'), billing_day=1,
        )
        out = StringIO()
        call_command('run_daily_jobs', '--tenant', self.tenant.slug, stdout=out)
        self.assertTrue(Invoice.objects.filter(tenant=self.tenant).exists())
        self.assertIn('invoices raised', out.getvalue())

    def test_document_expiry_alerts_fire_once_per_window(self):
        from django.core.management import call_command
        from io import StringIO

        MessageTemplate.objects.filter(tenant=self.tenant).update(is_approved=True)
        GuestDocument.objects.create(
            tenant=self.tenant, guest=self.guest, doc_type=GuestDocument.TYPE_VISA,
            number='V9', expiry_date=timezone.localdate() + timedelta(days=30))
        call_command('run_daily_jobs', '--tenant', self.tenant.slug, stdout=StringIO())
        self.assertEqual(MessageLog.objects.filter(
            tenant=self.tenant, template_code='document_expiry').count(), 1)
        call_command('run_daily_jobs', '--tenant', self.tenant.slug, stdout=StringIO())
        self.assertEqual(MessageLog.objects.filter(
            tenant=self.tenant, template_code='document_expiry').count(), 1)
