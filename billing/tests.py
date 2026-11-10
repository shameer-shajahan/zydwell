"""Invoicing, payments, receipts, reversal and the POS output."""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from accounting.models import KIND_INCOME, Category, LedgerEntry
from billing import receipts, services
from billing.models import Invoice, LateFeeRule, Payment, PrinterConfig
from core.tests import ZyDwellTestCase
from guests import services as guest_services


class InvoiceTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 1, 1),
            monthly_rent=Decimal('900'), billing_day=1,
        )

    def test_an_invoice_totals_rent_plus_extras_minus_discount(self):
        category = Category.by_code(self.tenant, KIND_INCOME, 'utilities_share')
        future = timezone.localdate() + timedelta(days=10)
        invoice = services.build_invoice(
            self.tenancy, period_start=future, period_end=future + timedelta(days=29),
            due_date=future, user=self.owner,
            extras=[{'category': category, 'description': 'Wi-Fi', 'amount': Decimal('50')}],
        )
        self.assertEqual(invoice.rent_amount, Decimal('900'))
        self.assertEqual(invoice.extras_total, Decimal('50'))
        self.assertEqual(invoice.total, Decimal('950'))
        self.assertEqual(invoice.status.code, 'pending')

        invoice.discount_amount = Decimal('100')
        invoice.save()
        invoice.recalculate()
        self.assertEqual(invoice.total, Decimal('850'))

    def test_invoice_numbers_are_unique_and_prefixed(self):
        first = services.build_invoice(self.tenancy, period_start=date(2026, 2, 1),
                                       period_end=date(2026, 2, 28), user=self.owner)
        second = services.build_invoice(self.tenancy, period_start=date(2026, 3, 1),
                                        period_end=date(2026, 3, 31), user=self.owner)
        self.assertNotEqual(first.number, second.number)
        self.assertTrue(first.number.startswith('INV'))

    def test_a_pro_rata_first_invoice_charges_only_the_days_stayed(self):
        late = guest_services.check_in(
            tenant=self.tenant, user=self.owner,
            guest=self.guest.__class__.objects.create(
                tenant=self.tenant, full_name='Late Arrival', mobile='0500000000'),
            room=self.room, bed=self.beds[1], check_in_date=date(2026, 2, 16),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        invoice = services.build_invoice(
            late, period_start=date(2026, 2, 16), period_end=date(2026, 2, 28),
            user=self.owner, prorata=True,
        )
        # 16–28 February is 13 days of a 28-day month: 900 x 13/28. February
        # days cost more than July days, which is the point of using the real
        # month rather than a flat 30.
        self.assertEqual(invoice.rent_amount, Decimal('417.86'))
        self.assertLess(invoice.rent_amount, late.monthly_rent)
        self.assertTrue(invoice.is_prorata)

    def test_generate_due_invoices_is_idempotent(self):
        first_run = services.generate_due_invoices(self.tenant, on_date=date(2026, 4, 3))
        second_run = services.generate_due_invoices(self.tenant, on_date=date(2026, 4, 3))
        self.assertEqual(len(first_run), 1)
        self.assertEqual(second_run, [])

    def test_aging_buckets_group_by_how_late_the_invoice_is(self):
        today = timezone.localdate()
        services.build_invoice(self.tenancy, period_start=today - timedelta(days=75),
                               period_end=today - timedelta(days=46),
                               due_date=today - timedelta(days=75), user=self.owner)
        services.build_invoice(self.tenancy, period_start=today - timedelta(days=15),
                               period_end=today + timedelta(days=14),
                               due_date=today - timedelta(days=15), user=self.owner)
        buckets = services.aging_buckets(self.tenant)
        self.assertEqual(buckets['60+'], Decimal('900'))
        self.assertEqual(buckets['0-30'], Decimal('900'))


class PaymentTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 1, 1),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        self.invoice = services.build_invoice(
            self.tenancy, period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            user=self.owner)

    def test_a_partial_payment_leaves_the_invoice_partly_paid(self):
        services.record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                                amount=Decimal('400'), invoice=self.invoice)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, Decimal('400'))
        self.assertEqual(self.invoice.balance, Decimal('500'))
        self.assertEqual(self.invoice.status.code, 'partly_paid')

    def test_paying_in_full_settles_the_invoice(self):
        services.record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                                amount=Decimal('900'), invoice=self.invoice)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status.code, 'paid')
        self.assertTrue(self.invoice.status.counts_as_settled)
        self.assertTrue(self.invoice.is_settled)

    def test_every_receipt_gets_its_own_number(self):
        numbers = set()
        for _step in range(5):
            payment = services.record_payment(
                tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                amount=Decimal('10'), invoice=self.invoice)
            numbers.add(payment.receipt_number)
        self.assertEqual(len(numbers), 5)

    def test_a_payment_posts_income_to_the_ledger(self):
        payment = services.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
            amount=Decimal('900'), invoice=self.invoice)
        entry = LedgerEntry.objects.get(source_type=LedgerEntry.SOURCE_PAYMENT,
                                        source_id=str(payment.pk))
        self.assertEqual(entry.amount, Decimal('900'))
        self.assertEqual(entry.kind, KIND_INCOME)
        self.assertEqual(entry.room, self.room)
        self.assertEqual(entry.building, self.building)

    def test_reversing_a_payment_unwinds_the_ledger_and_keeps_the_number(self):
        payment = services.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
            amount=Decimal('900'), invoice=self.invoice)
        number = payment.receipt_number
        services.reverse_payment(payment=payment, user=self.owner, reason='Cheque bounced')

        payment.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertTrue(payment.is_reversed)
        self.assertEqual(payment.receipt_number, number)
        self.assertEqual(self.invoice.paid_amount, Decimal('0'))
        self.assertFalse(LedgerEntry.objects.filter(
            source_type=LedgerEntry.SOURCE_PAYMENT, source_id=str(payment.pk)).exists())

    def test_only_an_admin_may_reverse_a_payment(self):
        payment = services.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
            amount=Decimal('900'), invoice=self.invoice)
        self.grant('billing', can_view=True, can_create=True, can_edit=True, can_delete=True)
        self.client.login(username='staff', password='pw-staff-123')
        response = self.client.post(f'/billing/payments/{payment.pk}/reverse/',
                                    {'reason': 'nope'})
        self.assertEqual(response.status_code, 403)
        payment.refresh_from_db()
        self.assertFalse(payment.is_reversed)

    def test_an_advance_becomes_credit_and_can_settle_a_later_invoice(self):
        services.record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                                amount=Decimal('500'), invoice=None)
        self.assertEqual(self.guest.advance_credit(), Decimal('500'))

        services.settle_from_credit(invoice=self.invoice, user=self.owner)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, Decimal('500'))
        self.assertEqual(self.guest.advance_credit(), Decimal('0'))

    def test_overpaying_an_invoice_is_refused_by_the_form(self):
        from billing.forms import PaymentForm

        form = PaymentForm(
            data={'tenancy': self.tenancy.pk, 'invoice': self.invoice.pk, 'amount': '5000',
                  'mode': 'cash', 'paid_on': timezone.localdate()},
            tenant=self.tenant, user=self.owner,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('amount', form.errors)

    def test_the_collector_name_is_stored_on_the_receipt(self):
        payment = services.record_payment(
            tenant=self.tenant, user=self.staff, tenancy=self.tenancy,
            amount=Decimal('100'), invoice=self.invoice)
        self.assertEqual(payment.collected_by_name, self.staff.display_name())

    def test_collection_summary_totals_by_mode(self):
        services.record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                                amount=Decimal('100'), invoice=self.invoice, mode='cash')
        services.record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                                amount=Decimal('200'), invoice=self.invoice, mode='bank',
                                reference='TRX-9')
        # "Cash in hand" follows the flag on the mode, not its name.
        today = timezone.localdate()
        summary = services.collection_summary(self.tenant, today, today)
        self.assertEqual(summary['total'], Decimal('300'))
        self.assertEqual(summary['cash'], Decimal('100'))
        self.assertEqual(summary['count'], 2)
        self.assertEqual(summary['by_mode']['Cash'], Decimal('100'))
        self.assertEqual(summary['by_mode']['Bank transfer'], Decimal('200'))


class LateFeeTests(ZyDwellTestCase):
    def test_a_fixed_late_fee_applies_after_the_grace_period(self):
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=40),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        due = timezone.localdate() - timedelta(days=20)
        invoice = services.build_invoice(
            tenancy, period_start=due, period_end=due + timedelta(days=29),
            due_date=due, user=self.owner)
        LateFeeRule.objects.filter(tenant=self.tenant).update(
            is_active=True, grace_days=5, kind=LateFeeRule.KIND_FIXED, value=Decimal('50'))

        services.apply_late_fees(self.tenant)
        invoice.refresh_from_db()
        self.assertEqual(invoice.late_fee, Decimal('50'))
        self.assertEqual(invoice.total, Decimal('950'))
        # The fee is counted once: on the invoice, not also as a line.
        self.assertEqual(invoice.extras_total, Decimal('0'))

    def test_no_fee_inside_the_grace_period(self):
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=5),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        invoice = services.build_invoice(
            tenancy, period_start=timezone.localdate() - timedelta(days=2),
            period_end=timezone.localdate() + timedelta(days=27),
            due_date=timezone.localdate() - timedelta(days=2), user=self.owner)
        LateFeeRule.objects.filter(tenant=self.tenant).update(
            is_active=True, grace_days=5, value=Decimal('50'))
        services.apply_late_fees(self.tenant)
        invoice.refresh_from_db()
        self.assertEqual(invoice.late_fee, Decimal('0'))


class ReceiptTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 1, 1), monthly_rent=Decimal('900'))
        invoice = services.build_invoice(tenancy, period_start=date(2026, 2, 1),
                                         period_end=date(2026, 2, 28), user=self.owner)
        self.payment = services.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=tenancy, amount=Decimal('900'),
            invoice=invoice, mode='cash')
        self.printer = PrinterConfig.objects.get(tenant=self.tenant)

    def test_the_text_receipt_fits_the_paper_width(self):
        for width, columns in (('58', 32), ('80', 48)):
            self.printer.paper_width = width
            self.printer.save()
            lines = receipts.receipt_lines(self.payment, self.printer)
            self.assertTrue(all(len(line) <= columns for line in lines),
                            f'a line overflowed {columns} columns')

    def test_the_receipt_carries_every_required_field(self):
        text = receipts.render_text(self.payment, self.printer)
        for needle in [self.building.name.upper(), self.payment.receipt_number,
                       self.guest.full_name, self.room.number, self.beds[0].label,
                       'AMOUNT PAID', 'Collected by']:
            self.assertIn(needle, text)

    def test_escpos_output_initialises_and_cuts(self):
        payload = receipts.render_escpos(self.payment, self.printer)
        self.assertTrue(payload.startswith(receipts.INIT))
        self.assertTrue(payload.endswith(receipts.CUT))

    def test_an_unreachable_printer_fails_softly(self):
        self.printer.connection = PrinterConfig.CONNECTION_NETWORK
        self.printer.address = '127.0.0.1:1'
        self.printer.save()
        ok, note = receipts.send_to_printer(self.printer, b'x', timeout=1)
        self.assertFalse(ok)
        self.assertIn('unreachable', str(note).lower())

    def test_the_receipt_page_renders_for_the_owner(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get(f'/billing/payments/{self.payment.pk}/receipt/')
        self.assertContains(response, self.payment.receipt_number)


class DeleteBillingTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=date(2026, 1, 1),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        start = timezone.localdate()
        self.invoice = services.build_invoice(
            self.tenancy, period_start=start, period_end=start + timedelta(days=29),
            due_date=start + timedelta(days=5), user=self.owner)
        self.client.login(username='owner', password='pw-owner-123')

    def _pay(self, amount, invoice=None):
        return services.record_payment(tenant=self.tenant, user=self.owner,
                                       tenancy=self.tenancy, amount=Decimal(amount),
                                       invoice=invoice)

    def test_an_unpaid_invoice_is_deleted(self):
        self.client.post(f'/billing/invoices/{self.invoice.pk}/delete/')
        self.assertFalse(Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_an_invoice_with_payments_must_lose_them_first(self):
        self._pay('400', self.invoice)
        self.client.post(f'/billing/invoices/{self.invoice.pk}/delete/')
        self.assertTrue(Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_deleting_a_payment_reopens_the_invoice_and_clears_the_ledger(self):
        payment = self._pay('900', self.invoice)
        self.client.post(f'/billing/payments/{payment.pk}/delete/')
        self.assertFalse(Payment.objects.filter(pk=payment.pk).exists())
        self.assertFalse(LedgerEntry.objects.filter(
            source_type=LedgerEntry.SOURCE_PAYMENT, source_id=str(payment.pk)).exists())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, Decimal('0'))
        self.assertNotEqual(self.invoice.status.code, 'paid')

    def test_then_the_invoice_can_be_deleted(self):
        payment = self._pay('900', self.invoice)
        self.client.post(f'/billing/payments/{payment.pk}/delete/')
        self.client.post(f'/billing/invoices/{self.invoice.pk}/delete/')
        self.assertFalse(Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_a_spent_advance_cannot_be_deleted(self):
        advance = self._pay('500')
        services.settle_from_credit(invoice=self.invoice, user=self.owner)
        self.client.post(f'/billing/payments/{advance.pk}/delete/')
        self.assertTrue(Payment.objects.filter(pk=advance.pk).exists())

    def test_staff_cannot_delete_money_records(self):
        self.grant('billing', can_view=True, can_create=True, can_edit=True, can_delete=True)
        self.client.login(username='staff', password='pw-staff-123')
        response = self.client.post(f'/billing/invoices/{self.invoice.pk}/delete/')
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_the_owner_sees_delete_buttons(self):
        payment = self._pay('100', self.invoice)
        self.assertContains(self.client.get(f'/billing/invoices/{self.invoice.pk}/'),
                            f'/billing/invoices/{self.invoice.pk}/delete/')
        self.assertContains(self.client.get(f'/billing/payments/{payment.pk}/'),
                            f'/billing/payments/{payment.pk}/delete/')


class EditBillingTests(DeleteBillingTests):
    def _edit_payment(self, payment, **changes):
        data = {'amount': payment.amount, 'mode': payment.mode_id, 'reference': 'REF1',
                'cheque_due_date': '2030-01-01', 'paid_on': payment.paid_on, 'note': ''}
        data.update(changes)
        return self.client.post(f'/billing/payments/{payment.pk}/edit/', data)

    def test_editing_a_payment_amount_retotals_the_invoice_and_ledger(self):
        payment = self._pay('400', self.invoice)
        self._edit_payment(payment, amount='600')
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, Decimal('600'))
        ledger = LedgerEntry.objects.get(source_type=LedgerEntry.SOURCE_PAYMENT,
                                         source_id=str(payment.pk))
        self.assertEqual(ledger.amount, Decimal('600'))

    def test_a_payment_cannot_be_edited_beyond_the_invoice_total(self):
        payment = self._pay('400', self.invoice)
        response = self._edit_payment(payment, amount='5000')
        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal('400'))

    def _edit_invoice(self, **changes):
        inv = self.invoice
        data = {'issue_date': inv.issue_date, 'period_start': inv.period_start,
                'period_end': inv.period_end, 'due_date': inv.due_date,
                'rent_amount': inv.rent_amount, 'late_fee': inv.late_fee,
                'discount_amount': inv.discount_amount, 'discount_reason': '', 'notes': ''}
        data.update(changes)
        return self.client.post(f'/billing/invoices/{inv.pk}/edit/', data)

    def test_editing_the_rent_retotals_the_invoice(self):
        self._edit_invoice(rent_amount='1000')
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.total, Decimal('1000'))

    def test_an_invoice_cannot_drop_below_what_was_paid(self):
        self._pay('800', self.invoice)
        response = self._edit_invoice(rent_amount='500')
        self.assertContains(response, 'already paid')
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.rent_amount, Decimal('900'))

    def test_staff_cannot_edit_money_records(self):
        self.client.login(username='staff', password='pw-staff-123')
        response = self._edit_invoice(rent_amount='1')
        self.assertEqual(response.status_code, 403)

    def test_the_owner_sees_edit_buttons(self):
        payment = self._pay('100', self.invoice)
        self.assertContains(self.client.get(f'/billing/invoices/{self.invoice.pk}/'),
                            f'/billing/invoices/{self.invoice.pk}/edit/')
        self.assertContains(self.client.get(f'/billing/payments/{payment.pk}/'),
                            f'/billing/payments/{payment.pk}/edit/')
        self.assertEqual(self.client.get(f'/billing/payments/{payment.pk}/edit/').status_code, 200)
        self.assertEqual(self.client.get(f'/billing/invoices/{self.invoice.pk}/edit/').status_code, 200)
