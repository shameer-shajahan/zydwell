"""Dashboard figures, exports and the audit trail."""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from accounting.models import KIND_EXPENSE, Category, LedgerEntry
from billing import services as billing
from core.models import AuditLog
from core.tests import ZyDwellTestCase
from guests import services as guest_services

XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class DashboardTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=10),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        self.invoice = billing.build_invoice(
            self.tenancy, period_start=timezone.localdate() - timedelta(days=5),
            period_end=timezone.localdate() + timedelta(days=24),
            due_date=timezone.localdate() - timedelta(days=5), user=self.owner)
        billing.record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                               amount=Decimal('400'), invoice=self.invoice)
        LedgerEntry.objects.create(
            tenant=self.tenant, kind=KIND_EXPENSE,
            category=Category.by_code(self.tenant, KIND_EXPENSE, 'cleaning'),
            amount=Decimal('150'), building=self.building)

    def test_the_dashboard_shows_occupancy_and_money(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['occupancy']['total'], 4)
        self.assertEqual(response.context['occupancy']['occupied'], 1)
        self.assertEqual(response.context['collected'], Decimal('400'))
        self.assertEqual(response.context['pending'], Decimal('500'))
        self.assertEqual(response.context['money']['expense'], Decimal('150'))
        self.assertEqual(response.context['money']['net'], Decimal('250'))

    def test_the_twelve_month_trend_has_twelve_points(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/')
        self.assertEqual(len(response.context['trend']), 12)

    def test_rent_roll_totals_the_live_tenancies(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/reports/rent-roll/')
        self.assertEqual(response.context['total_rent'], Decimal('900'))
        self.assertEqual(response.context['total_due'], Decimal('500'))

    def test_profit_report_splits_per_room(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/reports/profit/')
        labels = {row['label'] for row in response.context['rows']}
        self.assertIn(str(self.room), labels)


class ExportTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(), monthly_rent=Decimal('800'))
        self.client.login(username='owner', password='pw-owner-123')

    def test_rent_roll_exports_as_excel(self):
        response = self.client.get('/export/rent-roll/')
        self.assertEqual(response['Content-Type'], XLSX)
        self.assertTrue(response.content.startswith(b'PK'))

    def test_rent_roll_exports_as_pdf(self):
        response = self.client.get('/export/rent-roll/?format=pdf')
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_every_module_exports_into_one_workbook(self):
        response = self.client.get('/export/everything/')
        self.assertEqual(response['Content-Type'], XLSX)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.ACTION_EXPORT).exists())

    def test_staff_without_the_reports_grant_cannot_export(self):
        self.client.logout()
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get('/export/rent-roll/').status_code, 403)


class AuditTests(ZyDwellTestCase):
    def test_a_rent_change_records_the_old_and_new_value(self):
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'), billing_day=1,
        )
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/guests/tenancies/{tenancy.pk}/edit/', {
            'monthly_rent': '1100', 'billing_cycle': 'monthly', 'billing_day': '1',
            'deposit_amount': '800', 'agreement_note': '', 'rent_mode': 'advance',
            'prorate_first_period': 'on', 'first_invoice_amount': '',
        })
        tenancy.refresh_from_db()
        self.assertEqual(tenancy.monthly_rent, Decimal('1100'))
        entry = AuditLog.objects.filter(model_name='Tenancy',
                                        action=AuditLog.ACTION_UPDATE).first()
        self.assertIn('monthly_rent', entry.changes)
        self.assertEqual(entry.changes['monthly_rent'], ['800', '1100'])
        self.assertEqual(entry.actor, self.owner)

    def test_staff_cannot_change_the_rent_even_with_the_edit_grant(self):
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'), billing_day=1,
        )
        self.grant('guests', can_view=True, can_edit=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.client.post(f'/guests/tenancies/{tenancy.pk}/edit/', {
            'monthly_rent': '5000', 'billing_cycle': 'monthly', 'billing_day': '1',
            'deposit_amount': '800', 'agreement_note': '', 'rent_mode': 'advance',
            'prorate_first_period': 'on', 'first_invoice_amount': '',
        })
        tenancy.refresh_from_db()
        self.assertEqual(tenancy.monthly_rent, Decimal('800'))

    def test_signing_in_and_out_is_recorded(self):
        self.client.post('/accounts/login/',
                         {'username': 'owner', 'password': 'pw-owner-123'})
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.ACTION_LOGIN).exists())
        self.assertTrue(self.owner.login_history.filter(successful=True).exists())

    def test_a_failed_sign_in_is_recorded_too(self):
        from accounts.models import LoginHistory

        self.client.post('/accounts/login/', {'username': 'owner', 'password': 'wrong'})
        self.assertTrue(LoginHistory.objects.filter(successful=False).exists())

    def test_deleting_a_payment_source_is_traceable(self):
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(), monthly_rent=Decimal('800'))
        invoice = billing.build_invoice(
            tenancy, period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29), user=self.owner)
        payment = billing.record_payment(tenant=self.tenant, user=self.owner, tenancy=tenancy,
                                         amount=Decimal('800'), invoice=invoice)
        billing.reverse_payment(payment=payment, user=self.owner, reason='Bounced')
        entry = AuditLog.objects.filter(action=AuditLog.ACTION_REVERSE).first()
        self.assertIn(payment.receipt_number, entry.summary)


class StatusAdminTests(ZyDwellTestCase):
    def test_an_admin_can_add_a_status_from_the_settings_screen(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post('/settings/statuses/?kind=bed', {
            'kind': 'bed', 'code': 'quarantine', 'name': 'Quarantine', 'color': '#ff9500',
            'counts_as_occupied': 'on', 'sort_order': 9,
        })
        from core.models import Status

        status = Status.by_code(self.tenant, Status.KIND_BED, 'quarantine')
        self.assertIsNotNone(status)
        self.assertTrue(status.counts_as_occupied)

    def test_a_protected_status_cannot_be_deleted(self):
        from core.models import Status

        protected = Status.by_code(self.tenant, Status.KIND_BED, 'occupied')
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/settings/statuses/{protected.pk}/delete/')
        self.assertTrue(Status.objects.filter(pk=protected.pk).exists())

    def test_a_status_cannot_be_both_available_and_occupied(self):
        from reports.forms import StatusForm

        form = StatusForm(data={'kind': 'bed', 'code': 'weird', 'name': 'Weird',
                                'color': '#000000', 'counts_as_available': 'on',
                                'counts_as_occupied': 'on', 'sort_order': 1})
        self.assertFalse(form.is_valid())
