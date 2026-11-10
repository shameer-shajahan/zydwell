"""One end-to-end walk through the system over HTTP, as the staff actually use it."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from accounting.models import KIND_EXPENSE, Category, LedgerEntry
from billing.models import Invoice, Payment
from core.models import AuditLog
from core.tests import ZyDwellTestCase
from guests.models import Guest, Tenancy


class EndToEndTests(ZyDwellTestCase):
    """Owner sets up, staff runs the day, owner reads the month."""

    def setUp(self):
        super().setUp()
        for module in ['properties', 'guests', 'billing', 'accounting', 'reports', 'pos',
                       'messaging']:
            self.grant(module, can_view=True, can_create=True, can_edit=True)

    def assertNoFormErrors(self, response):
        form = response.context.get('form') if response.context else None
        if form is not None and getattr(form, 'errors', None):
            self.fail(f'the form was rejected: {form.errors.as_json()}')

    def test_a_full_month_of_operations(self):
        staff = self.client
        self.assertTrue(staff.login(username='staff', password='pw-staff-123'))

        # --- 1. the collector registers a walk-in guest -------------------
        response = staff.post('/guests/new/', {
            'full_name': 'Nadeem Akhtar', 'mobile': '0551112233',
            'nationality': 'Pakistani', 'employer': 'Transguard',
            'emergency_contact_name': 'Brother', 'emergency_contact_number': '0554445566',
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNoFormErrors(response)
        guest = Guest.objects.get(full_name='Nadeem Akhtar')
        self.assertEqual(guest.tenant, self.tenant)

        # --- 2. checks them into a bed ------------------------------------
        check_in_date = timezone.localdate() - timedelta(days=40)
        response = staff.post('/guests/tenancies/check-in/', {
            'guest': guest.pk, 'room': self.room.pk, 'bed': self.beds[0].pk,
            'check_in_date': check_in_date, 'monthly_rent': '900',
            'billing_cycle': 'monthly', 'billing_day': '1',
            'rent_mode': 'advance', 'first_charge': 'prorata', 'first_amount': '',
            'deposit_amount': '900', 'deposit_received': '900',
            'raise_first_invoice': 'on', 'agreement_note': '',
        }, follow=True)
        self.assertNoFormErrors(response)

        tenancy = Tenancy.objects.get(guest=guest)
        self.beds[0].refresh_from_db()
        self.assertEqual(self.beds[0].status.code, 'occupied')
        self.assertEqual(tenancy.deposit_held(), Decimal('900'))

        invoice = Invoice.objects.filter(tenancy=tenancy).first()
        self.assertIsNotNone(invoice, 'the first invoice should have been raised')
        # Joined mid-period, so the first invoice is pro-rata at 900/30 a day.
        self.assertTrue(invoice.is_prorata)
        billed_days = (invoice.period_end - invoice.period_start).days + 1
        self.assertEqual(invoice.rent_amount, tenancy.prorata(billed_days))
        self.assertLess(invoice.rent_amount, tenancy.monthly_rent)

        # --- 3. collects a part payment and prints the receipt ------------
        part = (invoice.balance / 2).quantize(Decimal('0.01'))
        from billing.models import PaymentMode

        cash = PaymentMode.by_code(self.tenant, 'cash')
        response = staff.post('/billing/payments/new/', {
            'tenancy': tenancy.pk, 'invoice': invoice.pk, 'amount': str(part),
            'mode': cash.pk, 'reference': '', 'paid_on': timezone.localdate(), 'note': '',
        }, follow=True)
        self.assertNoFormErrors(response)
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, part)
        self.assertEqual(invoice.status.code, 'partly_paid')

        # The collector is staff, so the receipt is provisional until the owner
        # confirms it — the guest's balance moves either way.
        payment_now = Payment.objects.get(tenancy=tenancy)
        self.assertTrue(payment_now.awaits_approval)

        payment = Payment.objects.get(tenancy=tenancy)
        self.assertEqual(payment.collected_by, self.staff)
        receipt = staff.get(f'/billing/payments/{payment.pk}/receipt/')
        self.assertContains(receipt, payment.receipt_number)

        # …and the guest reads the same receipt from a link, with nothing to
        # download and nothing to change.
        from billing.sharing import receipt_url

        guest_view = self.client_class().get(receipt_url(payment))
        self.assertEqual(guest_view.status_code, 200)
        self.assertIn(payment.receipt_number, guest_view.content.decode())
        self.assertNotIn('<form', guest_view.content.decode().lower())

        # the payment reached the income ledger
        self.assertTrue(LedgerEntry.objects.filter(
            source_type=LedgerEntry.SOURCE_PAYMENT, source_id=str(payment.pk)).exists())

        # --- 4. records an expense against the floor ----------------------
        response = staff.post('/accounting/new/', {
            'kind': KIND_EXPENSE,
            'category': Category.by_code(self.tenant, KIND_EXPENSE, 'maintenance').pk,
            'amount': '250', 'entry_date': timezone.localdate(),
            'description': 'Replaced the corridor light', 'floor': self.floor.pk,
            'payee': 'Local electrician', 'reference': '',
        }, follow=True)
        self.assertNoFormErrors(response)
        expense = LedgerEntry.objects.get(kind=KIND_EXPENSE, amount=Decimal('250'))
        self.assertEqual(expense.building, self.building)
        self.assertEqual(expense.recorded_by, self.staff)

        # --- 5. moves the guest to another bed mid-month ------------------
        response = staff.post(f'/guests/tenancies/{tenancy.pk}/transfer/', {
            'new_room': self.room.pk, 'new_bed': self.beds[2].pk,
            'transfer_date': timezone.localdate(), 'new_rent': '',
            'reason': 'Asked for a window bed',
        }, follow=True)
        self.assertNoFormErrors(response)
        tenancy.refresh_from_db()
        self.beds[0].refresh_from_db()
        self.beds[2].refresh_from_db()
        self.assertEqual(tenancy.bed, self.beds[2])
        self.assertEqual(tenancy.monthly_rent, self.beds[2].monthly_price)
        self.assertEqual(self.beds[0].status.code, 'available')
        self.assertEqual(self.beds[2].status.code, 'occupied')
        self.assertEqual(tenancy.transfers.count(), 1)

        # --- 6. staff cannot reverse the payment; the owner can -----------
        reverse_url = f'/billing/payments/{payment.pk}/reverse/'
        self.assertEqual(staff.post(reverse_url, {'reason': 'oops'}).status_code, 403)

        owner = self.client_class()
        owner.login(username='owner', password='pw-owner-123')
        owner.post(reverse_url, {'reason': 'Counted twice at handover'}, follow=True)
        payment.refresh_from_db()
        invoice.refresh_from_db()
        self.assertTrue(payment.is_reversed)
        self.assertEqual(invoice.paid_amount, Decimal('0'))
        self.assertFalse(LedgerEntry.objects.filter(
            source_type=LedgerEntry.SOURCE_PAYMENT, source_id=str(payment.pk)).exists())

        # --- 7. the guest pays in full -------------------------------------
        bank = PaymentMode.by_code(self.tenant, 'bank')
        response = staff.post('/billing/payments/new/', {
            'tenancy': tenancy.pk, 'invoice': invoice.pk, 'amount': str(invoice.balance),
            'mode': bank.pk, 'reference': 'TRX-77', 'paid_on': timezone.localdate(),
            'note': '',
        }, follow=True)
        self.assertNoFormErrors(response)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status.code, 'paid')
        self.assertTrue(invoice.is_settled)

        # --- 8. the owner reads the month ----------------------------------
        dashboard = owner.get('/')
        self.assertEqual(dashboard.context['occupancy']['occupied'], 1)
        self.assertEqual(dashboard.context['money']['expense'], Decimal('250'))

        # The transfer left no explicit rent, so the new bed's price took over.
        rent_roll = owner.get('/reports/rent-roll/')
        self.assertEqual(rent_roll.context['total_rent'], self.beds[2].monthly_price)

        workbook = owner.get('/export/everything/')
        self.assertTrue(workbook.content.startswith(b'PK'))

        # --- 9. and checks the guest out -----------------------------------
        response = owner.post(f'/guests/tenancies/{tenancy.pk}/check-out/', {
            'check_out_date': timezone.localdate(),
            'deduction_amount': '100', 'deduction_reason': 'Lost key card',
            'refund_amount': '800', 'note': 'Settled in cash',
        }, follow=True)
        self.assertNoFormErrors(response)
        tenancy.refresh_from_db()
        self.beds[2].refresh_from_db()
        self.assertEqual(tenancy.status.code, 'vacated')
        self.assertEqual(self.beds[2].status.code, 'available')
        self.assertEqual(tenancy.deposit_held(), Decimal('0'))

        # --- 10. everything of consequence is in the audit log -------------
        summaries = ' | '.join(AuditLog.objects.values_list('summary', flat=True))
        for needle in ['Checked in', 'Receipt', 'Reversed receipt', 'Transferred',
                       'Checked out']:
            self.assertIn(needle, summaries)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.ACTION_EXPORT).exists())
