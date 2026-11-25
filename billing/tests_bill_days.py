"""Guests staying from before move onto their check-in day, with one short invoice."""
from datetime import date
from decimal import Decimal

from django.urls import reverse

from billing import services
from billing.models import Invoice
from core.constants import MODULE_BILLING
from core.tests import ZyDwellTestCase
from guests import services as stays


class MoveToCheckInDayTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        # Checked in on 20 Sep, billed on the 1st, first invoice 20-30 Sep.
        self.tenancy = stays.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 9, 20), monthly_rent=Decimal('500'),
            billing_day=1)
        stays.raise_first_invoice(tenancy=self.tenancy, user=self.owner)

    def test_the_plan(self):
        plan = services.plan_bill_day(self.tenancy)
        self.assertEqual(plan['day'], 20)
        self.assertEqual((plan['start'], plan['end']), (date(2026, 10, 1), date(2026, 10, 19)))
        # 19 days of the 30-day period 20 Sep - 19 Oct.
        self.assertEqual(plan['rent'], Decimal('316.67'))

    def test_moving_adds_one_short_invoice_then_full_months(self):
        services.move_to_check_in_day(tenancy=self.tenancy, user=self.owner)
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.billing_day, 20)
        short = Invoice.objects.get(tenancy=self.tenancy, period_start=date(2026, 10, 1))
        self.assertEqual(short.period_end, date(2026, 10, 19))
        self.assertEqual(short.rent_amount, Decimal('316.67'))
        services.generate_due_invoices(self.tenant, on_date=date(2026, 11, 25))
        periods = list(Invoice.objects.filter(tenancy=self.tenancy).order_by('period_start')
                       .values_list('period_start', 'period_end', 'rent_amount'))
        self.assertEqual(periods[2:], [
            (date(2026, 10, 20), date(2026, 11, 19), Decimal('500.00')),
            (date(2026, 11, 20), date(2026, 12, 19), Decimal('500.00')),
        ])
        self.assertIsNone(services.plan_bill_day(self.tenancy))       # done once

    def test_a_stay_with_no_invoice_just_changes_day(self):
        Invoice.objects.filter(tenancy=self.tenancy).delete()
        services.move_to_check_in_day(tenancy=self.tenancy, user=self.owner)
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.billing_day, 20)
        self.assertFalse(Invoice.objects.filter(tenancy=self.tenancy).exists())

    def test_the_page_lists_then_moves(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('billing:bill_days'))
        self.assertContains(response, self.guest.full_name)
        self.assertContains(response, '316.67')
        self.client.post(reverse('billing:bill_days'))
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.billing_day, 20)

    def test_only_the_owner(self):
        self.grant(MODULE_BILLING, can_create=True, can_edit=True)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('billing:bill_days')).status_code, 403)
