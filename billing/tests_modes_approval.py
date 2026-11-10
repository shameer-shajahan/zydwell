"""Payment modes as data, and money a collector takes needing the owner's nod."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from accounting.models import LedgerEntry
from billing import services
from billing.models import Payment, PaymentMode
from core.models import AuditLog
from core.tests import ZyDwellTestCase
from guests import services as guest_services
from guests.models import DepositTransaction


class PaymentModeTests(ZyDwellTestCase):
    def test_every_client_starts_with_the_usual_ways_to_pay(self):
        codes = set(PaymentMode.objects.filter(tenant=self.tenant)
                    .values_list('code', flat=True))
        self.assertEqual(codes, {'cash', 'card', 'upi', 'bank', 'cheque', 'credit',
                                 'online'})

    def test_only_cash_counts_towards_the_handover(self):
        cash = PaymentMode.by_code(self.tenant, 'cash')
        upi = PaymentMode.by_code(self.tenant, 'upi')
        self.assertTrue(cash.counts_as_cash)
        self.assertFalse(upi.counts_as_cash)

    def test_an_owner_can_add_their_own(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post('/billing/modes/', {
            'name': 'Tabby', 'code': 'tabby', 'needs_reference': 'on',
            'sort_order': 9, 'is_active': 'on',
        })
        mode = PaymentMode.by_code(self.tenant, 'tabby')
        self.assertIsNotNone(mode)
        self.assertTrue(mode.needs_reference)

    def test_a_mode_already_used_is_switched_off_not_deleted(self):
        tenancy = self._tenancy()
        services.record_payment(tenant=self.tenant, user=self.owner, tenancy=tenancy,
                                amount=Decimal('50'), mode='upi', reference='UPI-1')
        upi = PaymentMode.by_code(self.tenant, 'upi')
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/billing/modes/{upi.pk}/delete/')
        upi.refresh_from_db()
        self.assertFalse(upi.is_active)

    def test_an_unused_mode_can_be_removed(self):
        upi = PaymentMode.by_code(self.tenant, 'upi')
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/billing/modes/{upi.pk}/delete/')
        self.assertIsNone(PaymentMode.by_code(self.tenant, 'upi'))

    def test_a_mode_that_needs_a_reference_insists_on_one(self):
        tenancy = self._tenancy()
        with self.assertRaises(ValidationError):
            services.record_payment(tenant=self.tenant, user=self.owner, tenancy=tenancy,
                                    amount=Decimal('50'), mode='upi')

    def test_a_cheque_also_needs_its_due_date(self):
        tenancy = self._tenancy()
        with self.assertRaises(ValidationError):
            services.record_payment(tenant=self.tenant, user=self.owner, tenancy=tenancy,
                                    amount=Decimal('50'), mode='cheque',
                                    reference='CHQ-1')

    def test_the_credit_mode_spends_the_guest_advance(self):
        tenancy = self._tenancy()
        payment = services.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=tenancy,
            amount=Decimal('50'), mode='credit')
        self.assertTrue(payment.from_credit)
        self.assertFalse(LedgerEntry.objects.filter(
            source_type=LedgerEntry.SOURCE_PAYMENT).exists())

    def test_a_deposit_records_how_it_arrived(self):
        tenancy = self._tenancy()
        entry = guest_services.record_deposit(
            tenancy=tenancy, user=self.owner, amount=Decimal('900'), mode='upi',
            reference='UPI-77')
        self.assertEqual(entry.mode.code, 'upi')
        self.assertEqual(entry.reference, 'UPI-77')

    def _tenancy(self):
        return guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'))


class ApprovalTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'))
        self.invoice = services.build_invoice(
            self.tenancy, period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29), user=self.owner)

    def collect(self, user):
        return services.record_payment(
            tenant=self.tenant, user=user, tenancy=self.tenancy,
            amount=Decimal('400'), invoice=self.invoice, mode='cash')

    # --- who needs approving ---------------------------------------------
    def test_the_owner_needs_nobody_to_approve_them(self):
        payment = self.collect(self.owner)
        self.assertEqual(payment.approval, Payment.APPROVAL_NOT_REQUIRED)

    def test_a_collector_does(self):
        payment = self.collect(self.staff)
        self.assertEqual(payment.approval, Payment.APPROVAL_PENDING)
        self.assertTrue(payment.awaits_approval)

    def test_unless_the_owner_has_trusted_them_with_it(self):
        self.grant('billing', can_view=True, can_create=True, can_approve=True)
        self.staff.refresh_from_db()
        payment = self.collect(self.staff)
        self.assertEqual(payment.approval, Payment.APPROVAL_NOT_REQUIRED)

    def test_the_money_still_counts_while_it_waits(self):
        """The guest did pay — chasing them for it would be wrong."""
        self.collect(self.staff)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, Decimal('400'))

    # --- deciding ----------------------------------------------------------
    def test_approving_names_the_owner_and_the_moment(self):
        payment = self.collect(self.staff)
        services.approve_payment(payment=payment, user=self.owner, note='Counted')
        payment.refresh_from_db()
        self.assertEqual(payment.approval, Payment.APPROVAL_APPROVED)
        self.assertEqual(payment.approved_by, self.owner)
        self.assertIsNotNone(payment.approved_at)
        self.assertTrue(AuditLog.objects.filter(summary__contains='Approved receipt').exists())

    def test_rejecting_reverses_it_and_puts_the_debt_back(self):
        payment = self.collect(self.staff)
        services.reject_payment(payment=payment, user=self.owner,
                                reason='Never reached the office')
        payment.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertEqual(payment.approval, Payment.APPROVAL_REJECTED)
        self.assertTrue(payment.is_reversed)
        self.assertEqual(self.invoice.paid_amount, Decimal('0'))
        self.assertFalse(LedgerEntry.objects.filter(
            source_type=LedgerEntry.SOURCE_PAYMENT, source_id=str(payment.pk)).exists())

    def test_the_receipt_number_is_still_never_reused(self):
        payment = self.collect(self.staff)
        retired = payment.receipt_number
        services.reject_payment(payment=payment, user=self.owner, reason='No')
        fresh = self.collect(self.owner)
        self.assertNotEqual(fresh.receipt_number, retired)

    # --- the screen ---------------------------------------------------------
    def test_only_the_owner_sees_the_approvals_screen(self):
        self.grant('billing', can_view=True, can_create=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get('/billing/approvals/').status_code, 403)

    def test_the_owner_sees_what_is_waiting(self):
        self.collect(self.staff)
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/billing/approvals/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], Decimal('400'))
        self.assertContains(response, self.staff.display_name())

    def test_approving_from_the_screen(self):
        payment = self.collect(self.staff)
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/billing/approvals/payment/{payment.pk}/approve/')
        payment.refresh_from_db()
        self.assertEqual(payment.approval, Payment.APPROVAL_APPROVED)

    def test_rejecting_needs_a_reason(self):
        payment = self.collect(self.staff)
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/billing/approvals/payment/{payment.pk}/reject/', {'reason': ''})
        payment.refresh_from_db()
        self.assertTrue(payment.awaits_approval)

    def test_the_dashboard_tells_the_owner_money_is_waiting(self):
        self.collect(self.staff)
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/')
        self.assertEqual(response.context['awaiting_count'], 1)
        self.assertEqual(response.context['awaiting_total'], Decimal('400'))
        self.assertContains(response, 'waiting on you')

    # --- deposits travel the same road -------------------------------------
    def test_a_deposit_a_collector_takes_also_waits(self):
        entry = guest_services.record_deposit(
            tenancy=self.tenancy, user=self.staff, amount=Decimal('900'))
        self.assertTrue(entry.awaits_approval)

    def test_rejecting_a_deposit_removes_it(self):
        entry = guest_services.record_deposit(
            tenancy=self.tenancy, user=self.staff, amount=Decimal('900'))
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/billing/approvals/deposit/{entry.pk}/reject/',
                         {'reason': 'Never handed over'})
        self.assertFalse(DepositTransaction.objects.filter(pk=entry.pk).exists())
        self.assertEqual(self.tenancy.deposit_held(), Decimal('0'))
        self.assertTrue(AuditLog.objects.filter(
            summary__contains='Rejected deposit').exists())
