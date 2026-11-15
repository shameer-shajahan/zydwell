"""Charging and collecting the part-month rent when a guest leaves mid-period."""
from datetime import date
from decimal import Decimal
from unittest import mock

from billing import services
from billing.models import Invoice, Payment, PaymentMode
from core.tests import ZyDwellTestCase
from guests import services as guest_services
from guests.models import Tenancy

TODAY = date(2026, 11, 15)


class FinalRentTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch('django.utils.timezone.localdate', return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cash = PaymentMode.by_code(self.tenant, 'cash')

    def check_in(self, **extra):
        values = dict(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 10, 1),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        values.update(extra)
        self.tenancy = guest_services.check_in(**values)
        self.url = f'/guests/tenancies/{self.tenancy.pk}/check-out/'
        return self.tenancy

    def post(self, **extra):
        data = {'check_out_date': TODAY.isoformat(), 'final_charge': 'keep',
                'final_amount': '', 'collect_amount': '', 'collect_mode': self.cash.pk,
                'collect_reference': '', 'deduction_amount': '0', 'deduction_reason': '',
                'refund_amount': '0', 'note': ''}
        data.update(extra)
        return self.client.post(self.url, data)

    def november(self):
        return Invoice.objects.get(tenancy=self.tenancy, period_start=date(2026, 11, 1))

    # --- advance billing: the full month is already on an invoice ------------

    def test_the_final_period_is_the_days_up_to_leaving(self):
        self.check_in()
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        final = services.final_period(self.tenancy, TODAY)
        self.assertEqual(final['period_start'], date(2026, 11, 1))
        self.assertEqual(final['period_end'], TODAY)
        self.assertEqual(final['days'], 15)
        self.assertEqual(final['period_days'], 30)
        self.assertEqual(final['prorata_rent'], Decimal('450.00'))
        self.assertEqual(final['invoice'], self.november())

    def test_owner_charges_only_the_days_stayed_and_collects_it(self):
        self.check_in()
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        self.client.login(username='owner', password='pw-owner-123')
        response = self.post(final_charge='prorata', collect_amount='450')
        self.assertEqual(response.status_code, 302)

        invoice = self.november()
        self.assertEqual(invoice.period_end, TODAY)
        self.assertEqual(invoice.rent_amount, Decimal('450.00'))
        self.assertEqual(invoice.total, Decimal('450.00'))
        self.assertEqual(invoice.balance, Decimal('0'))
        self.assertTrue(invoice.is_prorata)
        payment = invoice.payments.get()
        self.assertEqual(payment.amount, Decimal('450'))
        self.assertEqual(payment.approval, Payment.APPROVAL_NOT_REQUIRED)
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.status.code, 'vacated')
        self.assertEqual(self.tenancy.outstanding(), Decimal('0'))

    def test_an_agreed_amount_replaces_the_full_month(self):
        self.check_in()
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        self.client.login(username='owner', password='pw-owner-123')
        self.post(final_charge='custom', final_amount='500', collect_amount='300')
        invoice = self.november()
        self.assertEqual(invoice.total, Decimal('500.00'))
        self.assertEqual(invoice.paid_amount, Decimal('300'))
        self.assertEqual(invoice.balance, Decimal('200.00'))

    def test_no_change_keeps_the_full_month_as_before(self):
        self.check_in()
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        self.client.login(username='owner', password='pw-owner-123')
        self.post()
        invoice = self.november()
        self.assertEqual(invoice.total, Decimal('900.00'))
        self.assertEqual(invoice.period_end, date(2026, 11, 30))
        self.assertFalse(invoice.payments.exists())

    def test_the_rent_cannot_drop_below_what_is_already_paid(self):
        self.check_in()
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        services.record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                                invoice=self.november(), amount=Decimal('600'))
        self.client.login(username='owner', password='pw-owner-123')
        response = self.post(final_charge='prorata')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'has already been paid for this period')
        self.assertEqual(self.november().total, Decimal('900.00'))
        self.tenancy.refresh_from_db()
        self.assertTrue(self.tenancy.is_live)

    def test_collecting_more_than_is_owed_is_refused(self):
        self.check_in()
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        self.client.login(username='owner', password='pw-owner-123')
        response = self.post(final_charge='prorata', collect_amount='900')
        self.assertContains(response, 'more than the 450.00 owed')
        self.assertFalse(Payment.objects.exists())

    def test_an_agreed_amount_must_be_given(self):
        self.check_in()
        self.client.login(username='owner', password='pw-owner-123')
        response = self.post(final_charge='custom')
        self.assertContains(response, 'Enter the amount you agreed')

    # --- nothing billed yet for the final days -------------------------------

    def test_post_paid_final_days_get_their_own_invoice(self):
        self.check_in(rent_mode=Tenancy.MODE_POSTPAID)
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        self.assertFalse(Invoice.objects.filter(
            tenancy=self.tenancy, period_start=date(2026, 11, 1)).exists())
        self.client.login(username='owner', password='pw-owner-123')
        self.post(final_charge='prorata', collect_amount='450')

        invoice = self.november()
        self.assertEqual(invoice.period_end, TODAY)
        self.assertEqual(invoice.due_date, TODAY)
        self.assertEqual(invoice.total, Decimal('450.00'))
        self.assertEqual(invoice.balance, Decimal('0'))

    def test_the_nightly_run_does_not_bill_after_check_out(self):
        self.check_in()
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        self.client.login(username='owner', password='pw-owner-123')
        self.post(final_charge='prorata')
        self.assertEqual(services.generate_due_invoices(
            self.tenant, on_date=date(2026, 12, 1)), [])

    def test_a_stay_that_began_this_month_counts_from_check_in(self):
        self.check_in(check_in_date=date(2026, 11, 6))
        final = services.final_period(self.tenancy, TODAY)
        self.assertEqual(final['period_start'], date(2026, 11, 6))
        self.assertEqual(final['days'], 10)
        self.assertEqual(final['prorata_rent'], Decimal('300.00'))

    # --- staff ---------------------------------------------------------------

    def test_staff_can_set_the_final_rent_and_their_receipt_awaits_approval(self):
        self.check_in()
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        self.grant('guests', can_view=True, can_edit=True)
        self.grant('billing', can_view=True, can_create=True)
        self.client.login(username='staff', password='pw-staff-123')
        page = self.client.get(self.url)
        self.assertContains(page, 'Final period rent')

        self.assertEqual(self.post(final_charge='custom', final_amount='400',
                                   collect_amount='400').status_code, 302)
        invoice = self.november()
        self.assertEqual(invoice.total, Decimal('400.00'))
        payment = invoice.payments.get()
        self.assertEqual(payment.collected_by, self.staff)
        self.assertEqual(payment.approval, Payment.APPROVAL_PENDING)

    def test_staff_without_billing_rights_check_out_without_touching_invoices(self):
        self.check_in()
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        self.grant('guests', can_view=True, can_edit=True)
        self.client.login(username='staff', password='pw-staff-123')
        page = self.client.get(self.url)
        self.assertNotContains(page, 'Final period rent')

        self.post(final_charge='custom', final_amount='1', collect_amount='1')
        self.assertEqual(self.november().total, Decimal('900.00'))
        self.assertFalse(Payment.objects.exists())

    def test_a_tenancy_already_closed_cannot_be_checked_out_again(self):
        self.check_in()
        self.client.login(username='owner', password='pw-owner-123')
        self.post(final_charge='prorata')
        count = Invoice.objects.count()
        response = self.post(final_charge='prorata', check_out_date='2026-11-20')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Invoice.objects.count(), count)
