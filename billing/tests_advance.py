"""Collecting the next period's rent before it falls due."""
from datetime import date
from decimal import Decimal
from unittest import mock

from billing import services
from billing.models import Invoice, Payment, PaymentMode
from core.tests import ZyDwellTestCase
from guests import services as guest_services

TODAY = date(2026, 11, 14)


class AdvanceCollectionTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 10, 1),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        services.generate_due_invoices(self.tenant, on_date=date(2026, 10, 1))
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        self.url = f'/billing/payments/advance/{self.tenancy.pk}/'
        patcher = mock.patch('django.utils.timezone.localdate', return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, **extra):
        data = {'amount': '900', 'mode': PaymentMode.by_code(self.tenant, 'cash').pk,
                'paid_on': TODAY.isoformat(), 'reference': '', 'note': ''}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_the_next_period_is_the_one_after_the_last_invoice(self):
        terms = services.next_unbilled_period(self.tenancy)
        self.assertEqual(terms['period_start'], date(2026, 12, 1))
        self.assertEqual(terms['period_end'], date(2026, 12, 31))
        self.assertEqual(terms['due_date'], date(2026, 12, 1))
        self.assertEqual(services.rent_for_terms(self.tenancy, terms), Decimal('900'))

    def test_an_owner_collects_next_month_before_it_is_due(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.post()
        self.assertEqual(response.status_code, 302)

        invoice = Invoice.objects.get(tenancy=self.tenancy, period_start=date(2026, 12, 1))
        self.assertEqual(invoice.due_date, date(2026, 12, 1))
        self.assertEqual(invoice.paid_amount, Decimal('900'))
        self.assertEqual(invoice.balance, Decimal('0'))
        payment = invoice.payments.get()
        self.assertEqual(payment.paid_on, TODAY)
        self.assertEqual(payment.approval, Payment.APPROVAL_NOT_REQUIRED)

    def test_the_nightly_run_does_not_bill_a_month_already_collected(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.post()
        created = services.generate_due_invoices(self.tenant, on_date=date(2026, 12, 1))
        self.assertEqual(created, [])
        self.assertEqual(Invoice.objects.filter(
            tenancy=self.tenancy, period_start__gte=date(2026, 12, 1)).count(), 1)

    def test_paying_twice_moves_on_to_the_month_after(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.post()
        self.post()
        starts = list(Invoice.objects.filter(tenancy=self.tenancy).order_by(
            'period_start').values_list('period_start', flat=True))
        self.assertEqual(starts[-2:], [date(2026, 12, 1), date(2027, 1, 1)])

    def test_staff_with_collect_rights_can_take_it_and_it_awaits_approval(self):
        self.grant('billing', can_view=True, can_create=True)
        self.grant('guests', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        page = self.client.get(f'/guests/tenancies/{self.tenancy.pk}/')
        self.assertContains(page, self.url)

        self.assertEqual(self.post(amount='500').status_code, 302)
        payment = Payment.objects.get(invoice__period_start=date(2026, 12, 1))
        self.assertEqual(payment.collected_by, self.staff)
        self.assertEqual(payment.approval, Payment.APPROVAL_PENDING)
        self.assertEqual(payment.invoice.balance, Decimal('400'))

    def test_staff_without_collect_rights_are_refused(self):
        self.grant('billing', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.post().status_code, 403)
        self.assertFalse(Invoice.objects.filter(period_start=date(2026, 12, 1)).exists())

    def test_more_than_the_rent_is_refused_and_nothing_is_raised(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.post(amount='1800')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'more than the')
        self.assertFalse(Invoice.objects.filter(period_start=date(2026, 12, 1)).exists())

    def test_nothing_to_collect_after_the_planned_check_out(self):
        self.tenancy.expected_check_out = date(2026, 11, 30)
        self.tenancy.save()
        self.assertIsNone(services.next_unbilled_period(self.tenancy))
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_the_last_part_month_is_charged_pro_rata(self):
        self.tenancy.expected_check_out = date(2026, 12, 10)
        self.tenancy.save()
        terms = services.next_unbilled_period(self.tenancy)
        self.assertEqual(terms['period_end'], date(2026, 12, 10))
        self.assertTrue(terms['prorata'])
        # 10 of December's 31 days.
        self.assertEqual(services.rent_for_terms(self.tenancy, terms), Decimal('290.32'))
