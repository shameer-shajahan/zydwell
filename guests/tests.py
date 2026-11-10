"""Check-in, check-out, deposits and pro-rata bed transfers."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.tests import ZyDwellTestCase
from guests import services
from guests.models import DepositTransaction, Guest, GuestDocument, Tenancy
from properties.models import Bed


class CheckInTests(ZyDwellTestCase):
    def test_check_in_occupies_the_bed_and_opens_a_tenancy(self):
        tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 1, 10),
            monthly_rent=Decimal('800'), deposit_amount=Decimal('800'),
            deposit_received=Decimal('800'),
        )
        self.beds[0].refresh_from_db()
        self.assertEqual(self.beds[0].status.code, 'occupied')
        self.assertEqual(tenancy.status.code, 'active')
        self.assertEqual(tenancy.building, self.building)
        self.assertEqual(tenancy.floor, self.floor)
        self.assertEqual(tenancy.deposit_held(), Decimal('800'))

    def test_two_guests_cannot_share_one_bed(self):
        services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'),
        )
        other = Guest.objects.create(
            tenant=self.tenant, full_name='Second Guest', mobile='0509999999')
        with self.assertRaises(ValidationError):
            services.check_in(
                tenant=self.tenant, user=self.owner, guest=other, room=self.room,
                bed=self.beds[0], check_in_date=timezone.localdate(),
                monthly_rent=Decimal('800'),
            )

    def test_a_whole_flat_can_be_let_without_a_bed(self):
        tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.flat,
            bed=None, check_in_date=timezone.localdate(),
            monthly_rent=self.flat.monthly_rent,
        )
        self.assertIsNone(tenancy.bed)
        self.assertEqual(tenancy.monthly_rent, Decimal('5000'))

    def test_a_bed_from_another_room_is_rejected(self):
        other_bed = self.beds[0]
        tenancy = Tenancy(
            tenant=self.tenant, guest=self.guest, room=self.flat, bed=other_bed,
            check_in_date=timezone.localdate(), monthly_rent=Decimal('100'),
        )
        with self.assertRaises(ValidationError):
            tenancy.full_clean(exclude=['floor', 'building', 'status'])


class CheckOutTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=60),
            monthly_rent=Decimal('800'), deposit_amount=Decimal('800'),
            deposit_received=Decimal('800'),
        )

    def test_check_out_releases_the_bed_and_settles_the_deposit(self):
        services.check_out(
            tenancy=self.tenancy, user=self.owner,
            deductions=[{'amount': Decimal('150'), 'reason': 'Broken window'}],
            refund_amount=Decimal('650'),
        )
        self.beds[0].refresh_from_db()
        self.tenancy.refresh_from_db()
        self.assertEqual(self.beds[0].status.code, 'available')
        self.assertEqual(self.tenancy.status.code, 'vacated')
        self.assertIsNotNone(self.tenancy.actual_check_out)
        self.assertEqual(self.tenancy.deposit_held(), Decimal('0'))
        self.assertEqual(
            DepositTransaction.objects.filter(
                tenancy=self.tenancy, kind=DepositTransaction.KIND_DEDUCT).count(), 1)

    def test_you_cannot_pay_out_more_deposit_than_is_held(self):
        from guests.forms import CheckOutForm

        form = CheckOutForm(
            data={'check_out_date': timezone.localdate(), 'deduction_amount': '500',
                  'deduction_reason': 'Damage', 'refund_amount': '600'},
            tenancy=self.tenancy,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('exceed the deposit held', str(form.errors))

    def test_a_deduction_needs_a_reason(self):
        from guests.forms import CheckOutForm

        form = CheckOutForm(
            data={'check_out_date': timezone.localdate(), 'deduction_amount': '100',
                  'refund_amount': '0'},
            tenancy=self.tenancy,
        )
        self.assertFalse(form.is_valid())

    def test_notice_moves_the_tenancy_into_its_notice_period(self):
        services.give_notice(tenancy=self.tenancy, user=self.owner,
                             expected_check_out=timezone.localdate() + timedelta(days=30))
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.status.code, 'notice')
        self.assertTrue(self.tenancy.is_live)


class TransferTests(ZyDwellTestCase):
    def test_a_mid_month_move_splits_the_rent_pro_rata(self):
        tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 1, 1),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        self.beds[1].monthly_price = Decimal('1200')
        self.beds[1].save()

        record = services.transfer_bed(
            tenancy=tenancy, user=self.owner, new_bed=self.beds[1],
            transfer_date=date(2026, 3, 16), reason='Wanted a lower bunk',
        )
        tenancy.refresh_from_db()
        self.beds[0].refresh_from_db()
        self.beds[1].refresh_from_db()

        self.assertEqual(tenancy.bed, self.beds[1])
        self.assertEqual(tenancy.monthly_rent, Decimal('1200'))
        self.assertEqual(self.beds[0].status.code, 'available')
        self.assertEqual(self.beds[1].status.code, 'occupied')
        # 1–15 March on the old bed, 16–31 March on the new one. March has 31
        # days, so each slice is out of 31 — not out of a flat 30.
        self.assertEqual(record.old_days, 15)
        self.assertEqual(record.new_days, 16)
        self.assertEqual(record.old_prorata, Decimal('435.48'))   # 900 x 15/31
        self.assertEqual(record.new_prorata, Decimal('619.35'))   # 1200 x 16/31

    def test_the_old_and_new_bed_are_both_kept_in_history(self):
        tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 1, 1), monthly_rent=Decimal('800'),
        )
        services.transfer_bed(tenancy=tenancy, user=self.owner, new_bed=self.beds[2],
                              transfer_date=date(2026, 2, 10))
        record = tenancy.transfers.first()
        self.assertEqual(record.from_bed, self.beds[0])
        self.assertEqual(record.to_bed, self.beds[2])


class ProrataTests(ZyDwellTestCase):
    """Rent is split by the real length of the month, and rounded exactly once."""

    def tenancy_for(self, rent='800', day=1):
        return Tenancy(tenant=self.tenant, guest=self.guest, room=self.room,
                       bed=self.beds[0], check_in_date=date(2026, 1, 1),
                       monthly_rent=Decimal(rent), billing_day=day,
                       billing_cycle=Tenancy.CYCLE_MONTHLY)

    def test_the_period_length_is_the_real_month(self):
        tenancy = self.tenancy_for()
        self.assertEqual(tenancy.period_length(date(2026, 2, 10)), 28)
        self.assertEqual(tenancy.period_length(date(2026, 4, 10)), 30)
        self.assertEqual(tenancy.period_length(date(2026, 7, 10)), 31)

    def test_a_whole_month_billed_pro_rata_is_exactly_the_rent(self):
        """Dividing by 30 charged 31/30ths of the rent every long month."""
        tenancy = self.tenancy_for()
        for period_days in (28, 30, 31):
            self.assertEqual(tenancy.prorata(period_days, period_days=period_days),
                             Decimal('800.00'))

    def test_it_rounds_once_at_the_end(self):
        """800/30 = 26.67; times 15 that is 400.05, which is not 400.00."""
        tenancy = self.tenancy_for()
        self.assertEqual(tenancy.prorata(15, period_days=30), Decimal('400.00'))
        self.assertEqual(tenancy.prorata(12, period_days=30), Decimal('320.00'))

    def test_a_transfer_splits_one_month_and_no_more(self):
        """The reported fault: a mid-July move invented 26.66 of extra rent."""
        for month, length in [(2, 28), (4, 30), (7, 31), (12, 31)]:
            with self.subTest(month=month):
                bed = Bed.objects.create(tenant=self.tenant, room=self.flat,
                                         label=f'M{month}')
                guest = Guest.objects.create(tenant=self.tenant,
                                             full_name=f'Mover {month}',
                                             mobile='0500000000')
                tenancy = services.check_in(
                    tenant=self.tenant, user=self.owner, guest=guest, room=self.room,
                    bed=self.beds[month % 4], check_in_date=date(2026, 1, 1),
                    monthly_rent=Decimal('800'), billing_day=1,
                )
                record = services.transfer_bed(
                    tenancy=tenancy, user=self.owner, new_bed=bed,
                    new_room=self.flat, new_rent=Decimal('800'),
                    transfer_date=date(2026, month, 15),
                )
                self.assertEqual(record.old_days + record.new_days, length)
                self.assertEqual(record.old_prorata + record.new_prorata,
                                 Decimal('800.00'),
                                 f'month {month} split does not total one month')
                services.check_out(tenancy=tenancy, user=self.owner)

    def test_a_first_invoice_charges_the_right_fraction_of_a_long_month(self):
        from billing.services import build_invoice

        tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 7, 20),
            monthly_rent=Decimal('800'), billing_day=1,
        )
        invoice = build_invoice(tenancy, period_start=date(2026, 7, 20),
                                period_end=date(2026, 7, 31), user=self.owner,
                                prorata=True)
        # 12 of July's 31 days, not 12 of a notional 30.
        self.assertEqual(invoice.rent_amount, Decimal('309.68'))
        self.assertLess(invoice.rent_amount, tenancy.monthly_rent)

    def test_weekly_and_daily_cycles_keep_scaling_a_monthly_figure(self):
        tenancy = self.tenancy_for()
        tenancy.billing_cycle = Tenancy.CYCLE_WEEKLY
        self.assertEqual(tenancy.period_length(date(2026, 7, 10)), 30)


class DocumentTests(ZyDwellTestCase):
    def test_days_to_expiry_and_expired_flag(self):
        soon = GuestDocument.objects.create(
            tenant=self.tenant, guest=self.guest, doc_type=GuestDocument.TYPE_VISA,
            number='V1', expiry_date=timezone.localdate() + timedelta(days=7))
        gone = GuestDocument.objects.create(
            tenant=self.tenant, guest=self.guest, doc_type=GuestDocument.TYPE_PASSPORT,
            number='P1', expiry_date=timezone.localdate() - timedelta(days=1))
        self.assertEqual(soon.days_to_expiry, 7)
        self.assertFalse(soon.is_expired)
        self.assertTrue(gone.is_expired)
        self.assertEqual(self.guest.expiring_documents(30).count(), 2)


class BillingPeriodTests(ZyDwellTestCase):
    def test_the_monthly_period_wraps_around_the_billing_day(self):
        tenancy = Tenancy(
            tenant=self.tenant, guest=self.guest, room=self.room, bed=self.beds[0],
            check_in_date=date(2026, 1, 20), monthly_rent=Decimal('800'), billing_day=5,
        )
        start, end = tenancy.period_for(date(2026, 3, 3))
        self.assertEqual(start, date(2026, 2, 5))
        self.assertEqual(end, date(2026, 3, 4))

        start, end = tenancy.period_for(date(2026, 3, 20))
        self.assertEqual(start, date(2026, 3, 5))
        self.assertEqual(end, date(2026, 4, 4))


class BillingArrangementTests(ZyDwellTestCase):
    """Not every owner splits the first month by the day."""

    def check_in_on(self, day, **kwargs):
        return services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=day, monthly_rent=Decimal('900'),
            billing_day=1, **kwargs)

    def test_the_whole_rent_can_be_taken_however_late_they_arrive(self):
        from billing.services import generate_due_invoices

        tenancy = self.check_in_on(date(2026, 3, 25), prorate_first_period=False)
        invoice = generate_due_invoices(self.tenant, on_date=date(2026, 3, 25))[0]
        self.assertEqual(invoice.rent_amount, Decimal('900'))
        self.assertFalse(invoice.is_prorata)

    def test_or_only_the_days_stayed(self):
        from billing.services import generate_due_invoices

        self.check_in_on(date(2026, 3, 25), prorate_first_period=True)
        invoice = generate_due_invoices(self.tenant, on_date=date(2026, 3, 25))[0]
        self.assertEqual(invoice.rent_amount, Decimal('203.23'))   # 900 x 7/31
        self.assertTrue(invoice.is_prorata)

    def test_or_an_amount_the_office_agreed(self):
        from billing.services import generate_due_invoices

        self.check_in_on(date(2026, 3, 25), first_invoice_amount=Decimal('500'))
        invoice = generate_due_invoices(self.tenant, on_date=date(2026, 3, 25))[0]
        self.assertEqual(invoice.rent_amount, Decimal('500'))

    def test_the_agreed_amount_applies_to_the_first_invoice_only(self):
        from billing.services import generate_due_invoices

        self.check_in_on(date(2026, 3, 25), first_invoice_amount=Decimal('500'))
        generate_due_invoices(self.tenant, on_date=date(2026, 3, 25))
        second = generate_due_invoices(self.tenant, on_date=date(2026, 4, 2))[0]
        self.assertEqual(second.rent_amount, Decimal('900'))

    def test_paying_in_advance_falls_due_as_the_period_opens(self):
        from billing.services import generate_due_invoices

        self.check_in_on(date(2026, 3, 1))
        invoice = generate_due_invoices(self.tenant, on_date=date(2026, 3, 1))[0]
        self.assertEqual(invoice.period_start, date(2026, 3, 1))
        self.assertEqual(invoice.due_date, date(2026, 3, 1))

    def test_post_paid_bills_the_period_that_just_finished(self):
        from billing.services import generate_due_invoices

        self.check_in_on(date(2026, 3, 1), rent_mode=Tenancy.MODE_POSTPAID)
        invoice = generate_due_invoices(self.tenant, on_date=date(2026, 4, 1))[0]
        self.assertEqual(invoice.period_start, date(2026, 3, 1))
        self.assertEqual(invoice.period_end, date(2026, 3, 31))
        # Collected at the end of the month they lived through.
        self.assertEqual(invoice.due_date, date(2026, 3, 31))
        self.assertEqual(invoice.rent_amount, Decimal('900'))

    def test_post_paid_raises_nothing_before_the_period_ends(self):
        from billing.services import generate_due_invoices

        self.check_in_on(date(2026, 3, 1), rent_mode=Tenancy.MODE_POSTPAID)
        self.assertEqual(generate_due_invoices(self.tenant, on_date=date(2026, 3, 15)), [])

    def test_a_mid_month_arrival_is_never_billed_twice(self):
        """The nightly job used to raise another invoice for the same month
        every time it ran, because it looked for the month's own start date."""
        from billing.models import Invoice
        from billing.services import generate_due_invoices

        tenancy = self.check_in_on(date(2026, 3, 18))
        for day in (18, 20, 25, 31):
            generate_due_invoices(self.tenant, on_date=date(2026, 3, day))
        self.assertEqual(Invoice.objects.filter(tenancy=tenancy).count(), 1)

    def test_the_next_month_is_still_raised_normally(self):
        from billing.models import Invoice
        from billing.services import generate_due_invoices

        tenancy = self.check_in_on(date(2026, 3, 18))
        generate_due_invoices(self.tenant, on_date=date(2026, 3, 18))
        generate_due_invoices(self.tenant, on_date=date(2026, 4, 1))
        periods = list(Invoice.objects.filter(tenancy=tenancy)
                       .order_by('period_start').values_list('period_start', flat=True))
        self.assertEqual(periods, [date(2026, 3, 18), date(2026, 4, 1)])
