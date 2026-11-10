"""Invoice generation, payment recording and credit handling."""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import (
    Case, DateField, DecimalField, ExpressionWrapper, F, Q, Sum, Value, When,
)
from django.utils import timezone
from django.utils.translation import gettext as _

from accounting.models import LedgerEntry
from core import audit
from core.models import Status
from guests.models import Tenancy

from .models import Invoice, InvoiceLine, LateFeeRule, Payment, PaymentMode

TWOPLACES = Decimal('0.01')


def build_invoice(tenancy, *, period_start, period_end, due_date=None, user=None,
                  rent_amount=None, extras=None, prorata=False, notes='',
                  period_days=None):
    """Create one invoice for one tenancy and one period.

    A pro-rata invoice charges ``period_end - period_start`` days out of the full
    billing period. The divisor is the whole period, not the slice being charged,
    so a part month stays a fraction of one month's rent.
    """
    if rent_amount is None:
        rent_amount = tenancy.monthly_rent
        if prorata:
            days = (period_end - period_start).days + 1
            rent_amount = tenancy.prorata(
                days, period_days=period_days or tenancy.period_length(period_start)
            )
    invoice = Invoice(
        tenant=tenancy.tenant,
        tenancy=tenancy,
        period_start=period_start,
        period_end=period_end,
        due_date=due_date or period_start,
        rent_amount=Decimal(rent_amount).quantize(TWOPLACES, rounding=ROUND_HALF_UP),
        is_prorata=prorata,
        notes=notes,
        created_by=user,
        status=Status.by_code(tenancy.tenant, Status.KIND_INVOICE, 'pending'),
    )
    invoice.save()
    for line in (extras or []):
        InvoiceLine.objects.create(
            invoice=invoice,
            category=line['category'],
            description=line['description'],
            amount=Decimal(line['amount']),
        )
    invoice.recalculate()
    audit.log(audit.AuditLog.ACTION_CREATE, invoice, tenant=tenancy.tenant,
              summary=_('Invoice %(no)s raised for %(guest)s (%(period)s)') % {
                  'no': invoice.number, 'guest': tenancy.guest.full_name,
                  'period': invoice.period_label()})
    return invoice


@transaction.atomic
def generate_due_invoices(tenant, on_date=None, user=None):
    """Raise the invoices that fall due today for every live tenancy.

    Safe to run repeatedly — a period already invoiced is skipped.
    """
    on_date = on_date or timezone.localdate()
    created = []
    tenancies = (
        Tenancy.objects.filter(tenant=tenant, status__code__in=Tenancy.LIVE_STATUS_CODES)
        .select_related('guest', 'room', 'floor', 'building')
    )
    for tenancy in tenancies:
        period_start, period_end = tenancy.billing_period_for(on_date)
        if period_end < tenancy.check_in_date:
            continue
        if tenancy.actual_check_out and period_start > tenancy.actual_check_out:
            continue
        # Match on any overlap. Checking the period's own start date misses an
        # invoice stored under a mid-month check-in date, and bills them again
        # on every run.
        if Invoice.objects.filter(
            tenancy=tenancy, period_start__lte=period_end, period_end__gte=period_start
        ).exists():
            continue

        effective_start = period_start
        effective_end = period_end
        part_period = False
        if tenancy.check_in_date > period_start:
            effective_start = tenancy.check_in_date
            part_period = True
        if tenancy.expected_check_out and tenancy.expected_check_out < period_end:
            effective_end = tenancy.expected_check_out
            part_period = True

        # A part period is only charged pro-rata when the tenancy says so; some
        # agreements take the whole month's rent whatever day they arrived.
        prorata = part_period and tenancy.prorate_first_period

        rent_amount = None
        if tenancy.first_invoice_amount is not None and \
                not Invoice.objects.filter(tenancy=tenancy).exists():
            rent_amount = tenancy.first_invoice_amount

        created.append(build_invoice(
            tenancy,
            period_start=effective_start,
            period_end=effective_end,
            due_date=tenancy.due_date_for(effective_start, period_end),
            user=user,
            rent_amount=rent_amount,
            prorata=prorata,
            period_days=tenancy.period_length(period_start),
        ))
    return created


@transaction.atomic
def resolve_mode(tenant, mode=None):
    """A PaymentMode, its code, or nothing at all — all end up as the right row."""
    from .models import PaymentMode

    if isinstance(mode, str):
        mode = PaymentMode.by_code(tenant, mode)
    return mode or PaymentMode.by_code(tenant, 'cash') or PaymentMode.default_for(tenant)


def needs_approval(user):
    """Money a collector takes in is provisional until the owner confirms it.

    The spec calls this "prevents unrecorded collections". An owner, or a staff
    member the owner has trusted with the approve right, does not need it.
    """
    if user is None or user.is_admin or user.is_super_admin:
        return False
    return not user.can('billing', 'approve')


def record_payment(*, tenant, user, tenancy, amount, mode=None, invoice=None,
                   reference='', paid_on=None, note='', cheque_due_date=None):
    """Take money in, number the receipt and post it to the income ledger."""
    from .models import PaymentMode

    mode = resolve_mode(tenant, mode)
    provisional = needs_approval(user)
    payment = Payment(
        tenant=tenant,
        tenancy=tenancy,
        invoice=invoice,
        amount=Decimal(amount).quantize(TWOPLACES, rounding=ROUND_HALF_UP),
        mode=mode,
        reference=reference,
        cheque_due_date=cheque_due_date,
        paid_on=paid_on or timezone.localdate(),
        note=note,
        collected_by=user,
        from_credit=bool(mode and mode.settles_from_credit),
        approval=Payment.APPROVAL_PENDING if provisional else Payment.APPROVAL_NOT_REQUIRED,
    )
    payment.full_clean(exclude=['receipt_number', 'collected_by_name'])
    payment.save()
    audit.log(audit.AuditLog.ACTION_CREATE, payment, tenant=tenant,
              summary=_('Receipt %(no)s — %(amount)s from %(guest)s by %(mode)s%(flag)s') % {
                  'no': payment.receipt_number, 'amount': payment.amount,
                  'guest': tenancy.guest.full_name, 'mode': payment.mode or '—',
                  'flag': _(' (awaiting approval)') if provisional else ''})
    return payment


@transaction.atomic
def approve_payment(*, payment, user, note=''):
    payment.approval = Payment.APPROVAL_APPROVED
    payment.approved_by = user
    payment.approved_at = timezone.now()
    payment.approval_note = note
    payment.save(update_fields=['approval', 'approved_by', 'approved_at',
                                'approval_note', 'updated_at'])
    audit.log(audit.AuditLog.ACTION_UPDATE, payment, tenant=payment.tenant,
              summary=_('Approved receipt %(no)s collected by %(who)s') % {
                  'no': payment.receipt_number,
                  'who': payment.collected_by_name or '—'})
    return payment


@transaction.atomic
def reject_payment(*, payment, user, reason):
    """Rejecting means the money never arrived, so the receipt is reversed."""
    payment.approval = Payment.APPROVAL_REJECTED
    payment.approved_by = user
    payment.approved_at = timezone.now()
    payment.approval_note = reason
    payment.save(update_fields=['approval', 'approved_by', 'approved_at',
                                'approval_note', 'updated_at'])
    payment.reverse(user, _('Rejected by the owner: %(reason)s') % {'reason': reason})
    audit.log(audit.AuditLog.ACTION_REVERSE, payment, tenant=payment.tenant,
              summary=_('Rejected receipt %(no)s collected by %(who)s: %(reason)s') % {
                  'no': payment.receipt_number,
                  'who': payment.collected_by_name or '—', 'reason': reason})
    return payment


@transaction.atomic
def reverse_payment(*, payment, user, reason):
    """Admin-only. Keeps the receipt number retired and unwinds the ledger."""
    before = audit.snapshot(payment, ['is_reversed', 'amount'])
    payment.reverse(user, reason)
    audit.log(audit.AuditLog.ACTION_REVERSE, payment, tenant=payment.tenant,
              summary=_('Reversed receipt %(no)s: %(reason)s') % {
                  'no': payment.receipt_number, 'reason': reason},
              changes=audit.diff(before, audit.snapshot(payment, ['is_reversed', 'amount'])))
    return payment


def delete_payment(*, payment, user):
    """Admin-only hard delete. Returns ``(done, reason)``.

    The income ledger line goes with it and the invoice is re-totalled. An
    advance that has already been spent from credit cannot go — delete the
    credit settlements first.
    """
    if payment.invoice_id is None and not payment.is_reversed:
        credit = payment.tenancy.guest.advance_credit()
        if credit - payment.amount < 0:
            return False, _('Part of this advance has already been used as credit. '
                            'Delete those credit payments first.')
    invoice = payment.invoice
    with transaction.atomic():
        audit.log(audit.AuditLog.ACTION_DELETE, payment, tenant=payment.tenant,
                  summary=_('Deleted receipt %(no)s (%(amount)s)') % {
                      'no': payment.receipt_number, 'amount': payment.amount})
        LedgerEntry.objects.filter(
            tenant=payment.tenant, source_type=LedgerEntry.SOURCE_PAYMENT,
            source_id=str(payment.pk),
        ).delete()
        payment.delete()
        if invoice is not None:
            invoice.recalculate()
    return True, ''


def delete_invoice(*, invoice, user):
    """Admin-only hard delete. Returns ``(done, reason)``."""
    if invoice.payments.exists():
        return False, _('This invoice has payments against it. Delete those payments first.')
    with transaction.atomic():
        audit.log(audit.AuditLog.ACTION_DELETE, invoice, tenant=invoice.tenant,
                  summary=_('Deleted invoice %(no)s (%(total)s)') % {
                      'no': invoice.number, 'total': invoice.total})
        invoice.delete()
    return True, ''


def settle_from_credit(*, invoice, user):
    """Use a guest's advance credit against an open invoice."""
    from .models import PaymentMode

    credit = invoice.guest.advance_credit()
    if credit <= 0 or invoice.balance <= 0:
        return None
    amount = min(credit, invoice.balance)
    payment = Payment(
        tenant=invoice.tenant, tenancy=invoice.tenancy, invoice=invoice,
        amount=amount, mode=PaymentMode.by_code(invoice.tenant, 'credit'),
        from_credit=True, note=_('Settled from advance credit'), collected_by=user,
    )
    payment.save()
    audit.log(audit.AuditLog.ACTION_CREATE, payment, tenant=invoice.tenant,
              summary=_('Applied %(amount)s advance credit to %(no)s') % {
                  'amount': amount, 'no': invoice.number})
    return payment


def apply_late_fees(tenant, on_date=None):
    rule = LateFeeRule.objects.filter(tenant=tenant, is_active=True).first()
    if rule is None:
        return []
    touched = []
    open_invoices = Invoice.objects.filter(tenant=tenant).exclude(status__counts_as_settled=True)
    for invoice in open_invoices:
        before = invoice.late_fee
        # The fee lives on Invoice.late_fee and is already inside the total — it is
        # deliberately NOT also written as an invoice line, which would double-count it.
        if invoice.apply_late_fee(rule) != before:
            audit.log(audit.AuditLog.ACTION_UPDATE, invoice, tenant=tenant,
                      summary=_('Late fee on %(no)s set to %(fee)s') % {
                          'no': invoice.number, 'fee': invoice.late_fee},
                      changes={'late_fee': [str(before), str(invoice.late_fee)]})
            touched.append(invoice)
    return touched


def collection_summary(tenant, start, end, staff=None, payments=None):
    """Totals a collector or owner needs at the end of a shift.

    ``payments`` narrows the base set — a list screen passes the receipts the
    user may see in the place they picked, so the totals match the rows.
    """
    qs = payments if payments is not None else Payment.objects.filter(tenant=tenant)
    qs = qs.filter(paid_on__range=(start, end), is_reversed=False)
    if staff is not None:
        qs = qs.filter(collected_by=staff)
    by_mode = {
        row['mode__name'] or _('Unspecified'): row['total']
        for row in qs.order_by().values('mode__name').annotate(total=Sum('amount'))
    }
    cash = qs.filter(mode__counts_as_cash=True).aggregate(
        s=Sum('amount'))['s'] or Decimal('0')
    return {
        'total': qs.aggregate(s=Sum('amount'))['s'] or Decimal('0'),
        'cash': cash,
        'by_mode': by_mode,
        'count': qs.count(),
        'pending': qs.filter(approval=Payment.APPROVAL_PENDING).count(),
        'payments': qs.select_related('tenancy__guest', 'collected_by', 'mode'),
    }


MONEY = DecimalField(max_digits=14, decimal_places=2)


def with_balance(invoices):
    """Adds ``due_balance`` (total − paid) so lists can filter and sort on it."""
    return invoices.annotate(due_balance=ExpressionWrapper(
        F('total') - F('paid_amount'), output_field=MONEY))


def owing(invoices):
    """Unpaid invoices, latest first by days late and then by size.

    Not yet due counts as zero days late, exactly like ``Invoice.days_overdue``.
    """
    today = timezone.localdate()
    return with_balance(invoices.exclude(status__counts_as_settled=True)).filter(
        due_balance__gt=0,
    ).annotate(late_since=Case(
        When(due_date__lt=today, then=F('due_date')), default=Value(today),
        output_field=DateField(),
    )).order_by('late_since', '-due_balance', 'pk')


def aging_buckets(tenant, tenancy=None, invoices=None):
    """Unpaid balances by how late they are, summed in the database.

    ``invoices`` narrows the base set (a staff member's floors, a picked place).
    """
    qs = invoices if invoices is not None else Invoice.objects.filter(tenant=tenant)
    qs = qs.exclude(status__counts_as_settled=True)
    if tenancy is not None:
        qs = qs.filter(tenancy=tenancy)
    today = timezone.localdate()
    month, two_months = today - timezone.timedelta(days=30), today - timezone.timedelta(days=60)
    bands = {
        'current': Q(due_date__gte=today),
        '0-30': Q(due_date__lt=today, due_date__gte=month),
        '31-60': Q(due_date__lt=month, due_date__gte=two_months),
        '60+': Q(due_date__lt=two_months),
    }
    sums = with_balance(qs).filter(due_balance__gt=0).aggregate(**{
        f'band_{index}': Sum('due_balance', filter=rule, output_field=MONEY)
        for index, rule in enumerate(bands.values())
    })
    return {label: sums[f'band_{index}'] or Decimal('0')
            for index, label in enumerate(bands)}


def outstanding_ledger_check(tenant):
    """Guard used by reports: income posted vs payments recorded."""
    payments = Payment.objects.filter(tenant=tenant, is_reversed=False, from_credit=False)
    posted = LedgerEntry.objects.filter(
        tenant=tenant, source_type=LedgerEntry.SOURCE_PAYMENT
    ).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    taken = payments.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    return {'posted': posted, 'collected': taken, 'difference': taken - posted}
