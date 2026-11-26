"""Changing a check-in date rebuilds the invoices; money is taken in part by owner and staff."""
from datetime import date, timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from billing import services
from billing.models import Invoice, Payment
from core.constants import MODULE_BILLING, MODULE_GUESTS
from core.tests import ZyDwellTestCase
from guests.models import Guest, Tenancy


class CheckInDateChangeTests(ZyDwellTestCase):
    """The owner's case: Shameer checked in today, then the date is put back to 1 August."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.client.force_login(self.owner)
        self.shameer = Guest.objects.create(tenant=self.tenant, full_name='Shameer',
                                            mobile='0551230000')
        self.client.post(reverse('guests:check_in'), {
            'guest': self.shameer.pk, 'room': self.room.pk, 'bed': self.beds[0].pk,
            'check_in_date': self.today.isoformat(), 'monthly_rent': '500',
            'billing_cycle': 'monthly', 'billing_day': str(min(self.today.day, 28)),
            'rent_mode': 'advance', 'first_charge': 'full', 'deposit_amount': '0',
            'deposit_received': '0', 'raise_first_invoice': 'on'})
        self.tenancy = Tenancy.objects.get(guest=self.shameer)
        self.start = date(self.today.year, 8, 1) if self.today.month > 8 else \
            date(self.today.year - 1, 8, 1)

    def edit(self, **changes):
        data = {'check_in_date': self.tenancy.check_in_date.isoformat(), 'monthly_rent': '500',
                'rent_mode': 'advance', 'billing_cycle': 'monthly',
                'billing_day': str(self.tenancy.billing_day), 'expected_check_out': '',
                'deposit_amount': '0', 'first_invoice_amount': '', 'agreement_note': ''}
        data.update(changes)
        return self.client.post(reverse('guests:tenancy_edit', args=[self.tenancy.pk]), data,
                                follow=True)

    def periods(self):
        return list(Invoice.objects.filter(tenancy=self.tenancy).order_by('period_start')
                    .values_list('period_start', 'total'))

    def test_every_month_from_the_new_check_in_to_now_is_invoiced(self):
        self.edit(check_in_date=self.start.isoformat())
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.billing_day, 1)
        periods = self.periods()
        self.assertEqual(periods[0], (self.start, Decimal('500.00')))
        self.assertEqual(periods[-1][0], self.today.replace(day=1))     # up to this month
        self.assertTrue(all(total == Decimal('500.00') for _start, total in periods))
        self.assertEqual(services.unbilled_rent(self.tenancy), [])
        # Each number carries the month it bills.
        august = Invoice.objects.get(tenancy=self.tenancy, period_start=self.start)
        self.assertIn(f'{self.start:%Y%m}-', august.number)

    def test_the_payment_screen_lists_every_month(self):
        self.edit(check_in_date=self.start.isoformat())
        response = self.client.get(reverse('billing:payment_create') + f'?tenancy={self.tenancy.pk}')
        for invoice in Invoice.objects.filter(tenancy=self.tenancy):
            self.assertContains(response, invoice.number)
        self.assertContains(response, 'data-fill-in')

    def test_a_paid_invoice_stays_and_the_months_before_it_are_billed(self):
        invoice = Invoice.objects.get(tenancy=self.tenancy)
        services.record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                                invoice=invoice, amount=Decimal('100'))
        self.edit(check_in_date=self.start.isoformat())
        invoice.refresh_from_db()
        self.assertEqual(invoice.period_start, self.today)          # untouched
        periods = self.periods()
        self.assertEqual(periods[0][0], self.start)
        # Back to back up to the kept invoice: no day lost, none billed twice.
        rows = list(Invoice.objects.filter(tenancy=self.tenancy).order_by('period_start'))
        for before, after in zip(rows, rows[1:]):
            self.assertEqual(after.period_start, before.period_end + timedelta(days=1))

    def test_moving_it_later_removes_the_months_before(self):
        self.edit(check_in_date=self.start.isoformat())
        later = self.start + timedelta(days=31)
        self.edit(check_in_date=later.isoformat())
        self.assertEqual(self.periods()[0][0], later)

    def test_staff_without_billing_rights_cannot_rebuild(self):
        self.grant(MODULE_GUESTS, can_edit=True)
        self.client.force_login(self.staff)
        response = self.edit(check_in_date=self.start.isoformat())
        self.assertContains(response, 'needs billing rights')
        self.assertEqual(len(self.periods()), 1)


class PartPaymentTests(ZyDwellTestCase):
    """Owner and staff can both take part of what is owed."""

    def setUp(self):
        super().setUp()
        from guests import services as stays
        today = timezone.localdate()
        self.tenancy = stays.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=today - timedelta(days=40), monthly_rent=Decimal('800'))
        services.generate_due_invoices(self.tenant, on_date=today)
        self.invoices = list(Invoice.objects.filter(tenancy=self.tenancy).order_by('period_start'))

    def pay(self, amount):
        return self.client.post(reverse('billing:payment_create'), {
            'tenancy': self.tenancy.pk, 'invoices': [i.pk for i in self.invoices],
            'amount': amount, 'mode': '', 'reference': '', 'cheque_due_date': '',
            'paid_on': timezone.localdate().isoformat(), 'note': ''})

    def mode(self):
        from billing.models import PaymentMode
        return PaymentMode.by_code(self.tenant, 'cash').pk

    def test_the_owner_takes_part(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('billing:payment_create'), {
            'tenancy': self.tenancy.pk, 'invoices': [i.pk for i in self.invoices],
            'amount': '1000', 'mode': self.mode(), 'paid_on': timezone.localdate().isoformat()})
        self.assertEqual(response.status_code, 302)
        self.invoices[0].refresh_from_db()
        self.invoices[1].refresh_from_db()
        self.assertEqual(self.invoices[0].balance, Decimal('0'))     # oldest first
        self.assertEqual(self.invoices[1].balance, Decimal('600.00'))

    def test_staff_take_part_and_it_waits_for_approval(self):
        self.grant(MODULE_BILLING, can_create=True)
        self.client.force_login(self.staff)
        response = self.client.post(reverse('billing:payment_create'), {
            'tenancy': self.tenancy.pk, 'invoices': [self.invoices[0].pk],
            'amount': '300', 'mode': self.mode(), 'paid_on': timezone.localdate().isoformat()})
        self.assertEqual(response.status_code, 302)
        payment = Payment.objects.get(tenancy=self.tenancy)
        self.assertEqual(payment.amount, Decimal('300'))
        self.assertTrue(payment.awaits_approval)


class StayPagePagingTests(ZyDwellTestCase):
    def test_thirty_a_page(self):
        from guests import services as stays
        tenancy = stays.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=500),
            monthly_rent=Decimal('800'))
        services.generate_due_invoices(self.tenant)
        self.client.force_login(self.owner)
        response = self.client.get(reverse('guests:tenancy_detail', args=[tenancy.pk]))
        self.assertEqual(len(response.context['invoices'].object_list),
                         min(30, Invoice.objects.filter(tenancy=tenancy).count()))


class RebuildButtonTests(CheckInDateChangeTests):
    """A stay left with old-cycle invoices (its date moved before invoices followed it)."""

    def test_rebuild_lines_the_invoices_up_with_check_in(self):
        # As if the date had been changed without the invoices following.
        Tenancy.objects.filter(pk=self.tenancy.pk).update(check_in_date=self.start, billing_day=1)
        self.tenancy.refresh_from_db()
        self.client.post(reverse('guests:rebuild_invoices', args=[self.tenancy.pk]))
        periods = self.periods()
        self.assertEqual(periods[0][0], self.start)
        self.assertTrue(all(start.day == 1 for start, _total in periods))
        numbers = list(Invoice.objects.filter(tenancy=self.tenancy).values_list('number', flat=True))
        # Pressing it again changes nothing, not even the numbers.
        self.client.post(reverse('guests:rebuild_invoices', args=[self.tenancy.pk]))
        self.assertEqual(sorted(numbers), sorted(Invoice.objects.filter(
            tenancy=self.tenancy).values_list('number', flat=True)))
