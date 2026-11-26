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

from .models import (
    Invoice, InvoiceLine, LateFeeRule, Payment, PaymentAllocation, PaymentMode,
)

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


def period_terms(tenancy, period_start, period_end, first_invoice=None):
    """How one billing period is charged: the dates, due date and rent rules.

    Shared by the nightly run and advance collection so a period collected
    early is billed exactly as the nightly job would have billed it.
    """
    effective_start, effective_end = period_start, period_end
    arriving = leaving = False
    if tenancy.check_in_date > period_start:
        effective_start = tenancy.check_in_date
        arriving = True
    if tenancy.expected_check_out and tenancy.expected_check_out < period_end:
        effective_end = tenancy.expected_check_out
        leaving = True

    # The month they arrive in is charged by the day only when the tenancy says
    # so; some agreements take the whole month's rent whatever day they came.
    # The month they leave in is always charged for the days stayed.
    prorata = leaving or (arriving and tenancy.prorate_first_period)

    # ``first_invoice`` lets a caller that already knows (``unbilled_rent``)
    # spare the question to the database.
    if first_invoice is None:
        first_invoice = not Invoice.objects.filter(tenancy=tenancy).exists()
    rent_amount = None
    if tenancy.first_invoice_amount is not None and first_invoice:
        rent_amount = tenancy.first_invoice_amount

    return {
        'period_start': effective_start,
        'period_end': effective_end,
        'due_date': tenancy.due_date_for(effective_start, period_end),
        'rent_amount': rent_amount,
        'prorata': prorata,
        'period_days': tenancy.period_length(period_start),
    }


def rent_for_terms(tenancy, terms):
    """The rent ``build_invoice`` would charge for ``period_terms`` output."""
    if terms['rent_amount'] is not None:
        amount = Decimal(terms['rent_amount'])
    elif terms['prorata']:
        days = (terms['period_end'] - terms['period_start']).days + 1
        amount = tenancy.prorata(days, period_days=terms['period_days'])
    else:
        amount = tenancy.monthly_rent
    return Decimal(amount).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def next_unbilled_period(tenancy):
    """The ``period_terms`` for the first period not yet invoiced, or ``None``.

    That is the period after the latest one already billed, or the period the
    stay starts in when nothing is billed yet. ``None`` once the stay has
    ended, or will have ended, before that period opens.
    """
    from django.db.models import Max

    last_end = Invoice.objects.filter(tenancy=tenancy).aggregate(
        last=Max('period_end'))['last']
    anchor = last_end + timezone.timedelta(days=1) if last_end else tenancy.check_in_date
    period_start, period_end = tenancy.period_for(anchor)
    # A hand-made invoice can end mid-period. The nightly run treats any overlap
    # as already billed, so move on to the first period nothing touches.
    while Invoice.objects.filter(tenancy=tenancy, period_start__lte=period_end,
                                 period_end__gte=period_start).exists():
        period_start, period_end = tenancy.period_for(period_end + timezone.timedelta(days=1))
    for leaving in (tenancy.actual_check_out, tenancy.expected_check_out):
        if leaving and period_start > leaving:
            return None
    return period_terms(tenancy, period_start, period_end)


@transaction.atomic
def collect_in_advance(*, tenant, user, tenancy, amount, mode=None, reference='',
                       paid_on=None, note='', cheque_due_date=None):
    """Raise the next unbilled period's invoice now and take payment against it.

    The nightly run later finds the period already invoiced and skips it, so
    the guest is never billed twice. Returns ``(invoice, payment)``.
    """
    terms = next_unbilled_period(tenancy)
    if terms is None:
        raise ValueError('Nothing left to bill for this stay.')
    invoice = build_invoice(
        tenancy, user=user,
        notes=_('Raised early to collect rent in advance.'), **terms)
    payment = record_payment(
        tenant=tenant, user=user, tenancy=tenancy, invoice=invoice, amount=amount,
        mode=mode, reference=reference, paid_on=paid_on, note=note,
        cheque_due_date=cheque_due_date,
    )
    return invoice, payment


def final_period(tenancy, leaving_on):
    """What the last, part period of a stay looks like when leaving on ``leaving_on``.

    The guest is charged from the start of the period that contains the
    leaving day (or their check-in, if later) up to and including that day.
    ``invoice`` is the one already raised for that period, if any, and
    ``clash`` is set when more than one invoice touches it, which is too
    tangled to settle automatically.
    """
    period_start, period_end = tenancy.period_for(leaving_on)
    start = max(period_start, tenancy.check_in_date)
    period_days = tenancy.period_length(period_start)
    days = max((leaving_on - start).days + 1, 0)
    touching = list(Invoice.objects.filter(
        tenancy=tenancy, period_start__lte=period_end, period_end__gte=period_start,
    ).order_by('period_start', 'pk'))
    invoice = touching[0] if len(touching) == 1 else None
    later = Invoice.objects.filter(tenancy=tenancy, period_start__gt=period_end)
    return {
        'period_start': start,
        'period_end': leaving_on,
        'full_period_end': period_end,
        'period_days': period_days,
        'days': days,
        'prorata_rent': tenancy.prorata(days, period_days=period_days),
        'invoice': invoice,
        'clash': len(touching) > 1,
        'later_invoices': list(later.order_by('period_start')),
    }


def final_invoice_floor(invoice):
    """The lowest rent the final invoice can take without going below what is paid."""
    if invoice is None:
        return Decimal('0')
    others = invoice.extras_total + invoice.late_fee - invoice.discount_amount
    return max(invoice.paid_amount - others, Decimal('0')).quantize(TWOPLACES)


@transaction.atomic
def settle_final_period(*, tenant, user, tenancy, leaving_on, rent_amount=None,
                        collect_amount=None, mode=None, reference='', note='',
                        cheque_due_date=None):
    """Charge the agreed rent for the part period a leaving guest stayed, and
    optionally take the money for it.

    An invoice already raised for that period (a full month billed in
    advance) is cut down to the days stayed and the agreed rent. When nothing
    was billed yet (post-paid, or the nightly run has not reached it) a new
    invoice is raised for those days. ``rent_amount`` of ``None`` leaves the
    period as already billed and only takes the money, if any.
    Returns ``(invoice_or_None, payment_or_None)``.
    """
    terms = final_period(tenancy, leaving_on)
    invoice = terms['invoice']
    if rent_amount is None:
        payment = None
        if collect_amount and invoice is not None:
            payment = record_payment(
                tenant=tenant, user=user, tenancy=tenancy, invoice=invoice,
                amount=collect_amount, mode=mode, reference=reference, note=note,
                cheque_due_date=cheque_due_date)
        return invoice, payment
    if terms['clash']:
        raise ValueError('More than one invoice covers the final period.')
    rent_amount = Decimal(rent_amount).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    if invoice is not None and rent_amount < final_invoice_floor(invoice):
        raise ValueError('The final rent cannot go below what is already paid.')
    full_start = tenancy.period_for(leaving_on)[0]
    is_prorata = terms['days'] < (terms['full_period_end'] - full_start).days + 1
    label = _('Final rent for %(days)s days, leaving on %(date)s') % {
        'days': terms['days'], 'date': leaving_on.strftime('%d %b %Y')}

    if invoice is None:
        invoice = build_invoice(
            tenancy, user=user, period_start=terms['period_start'],
            period_end=terms['period_end'],
            due_date=leaving_on, rent_amount=rent_amount, prorata=is_prorata,
            notes=label[:200],
        )
    else:
        tracked = ['period_start', 'period_end', 'rent_amount', 'total']
        before = audit.snapshot(invoice, tracked)
        invoice.period_start = min(invoice.period_start, terms['period_start'])
        invoice.period_end = leaving_on
        invoice.rent_amount = rent_amount
        invoice.is_prorata = is_prorata
        invoice.notes = label[:200]
        if invoice.due_date > leaving_on:
            invoice.due_date = leaving_on
        invoice.save(update_fields=['period_start', 'period_end', 'rent_amount',
                                    'is_prorata', 'notes', 'due_date', 'updated_at'])
        invoice.recalculate()
        audit.log(audit.AuditLog.ACTION_UPDATE, invoice, tenant=tenant,
                  summary=_('Final rent on %(no)s set to %(amount)s for %(guest)s') % {
                      'no': invoice.number, 'amount': rent_amount,
                      'guest': tenancy.guest.full_name},
                  changes=audit.diff(before, audit.snapshot(invoice, tracked)))

    payment = None
    if collect_amount:
        payment = record_payment(
            tenant=tenant, user=user, tenancy=tenancy, invoice=invoice,
            amount=collect_amount, mode=mode, reference=reference, note=note,
            cheque_due_date=cheque_due_date,
        )
    return invoice, payment


@transaction.atomic
def generate_due_invoices(tenant, on_date=None, user=None, only=None):
    """Raise the invoices that fall due today for every live tenancy.

    Safe to run repeatedly — a period already invoiced is skipped. ``only``
    narrows the run to some tenancies (a queryset of them, or their ids).
    """
    created, _finished = generate_due_invoices_within(tenant, on_date=on_date, user=user,
                                                      only=only)
    return created


def generate_due_invoices_within(tenant, seconds=None, on_date=None, user=None, only=None):
    """``generate_due_invoices`` that stops after ``seconds``: ``(created, finished)``.

    A page has about 30 seconds before the web server gives up on it, and the
    first run after missed months can raise many invoices. Each invoice is
    saved as it is made, so stopping early loses nothing: run again and it
    carries on from the last invoice of each stay.
    """
    import time

    deadline = time.monotonic() + seconds if seconds else None
    on_date = on_date or timezone.localdate()
    created = []
    tenancies = (
        Tenancy.objects.filter(tenant=tenant, status__code__in=Tenancy.LIVE_STATUS_CODES)
        .select_related('guest', 'room', 'floor', 'building')
    )
    if only is not None:
        tenancies = tenancies.filter(pk__in=only)
    for tenancy in tenancies:
        if deadline is not None and time.monotonic() > deadline:
            return created, False
        # Start from check-in even when no invoice has been raised yet. This
        # fills every billable period through the latest due period, so the
        # invoice and overdue lists show the complete unpaid history.
        raised = _catch_up(tenancy, on_date, user)
        if raised:
            created += raised
            apply_guest_credit(guest=tenancy.guest, user=user)
    return created, True


#: At most this many missed periods are billed for one stay in one run.
CATCH_UP_LIMIT = 36


#: How many billing periods one look at a stay walks through at most.
PERIOD_SCAN_LIMIT = 600


def _uncovered(start, end, spans):
    """The pieces of ``start``..``end`` that no ``(start, end)`` span covers."""
    one = timezone.timedelta(days=1)
    pieces, cursor = [], start
    for span_start, span_end in sorted(spans):
        if span_end < cursor or span_start > end:
            continue
        if span_start > cursor:
            pieces.append((cursor, min(span_start - one, end)))
        cursor = max(cursor, span_end + one)
        if cursor > end:
            break
    if cursor <= end:
        pieces.append((cursor, end))
    return pieces


def unbilled_rent(tenancy, on_date=None, spans=None, fill_gaps=False):
    """Rent a stay owes by ``on_date`` that no invoice covers, piece by piece.

    From its check-in date up to the period due by ``on_date`` (billed as a
    period opens when paid in advance, once it has ended when post-paid),
    every day no invoice covers is owed. A whole period is charged as its
    invoice would be (``period_terms``); a piece of one — the days before a
    first invoice, or after an invoice that was cut short — by the day. No
    day past the leaving date. Nothing is saved, so a page can show it.

    ``spans`` replaces the stay's invoices with ``(start, end)`` pairs (to see
    what a change would do). Gaps *between* invoices are left alone unless
    ``fill_gaps``: someone may have deleted an invoice on purpose. A team let
    starts with its first invoice, or the current period when it has none —
    teams are imported with joining dates long past and that rent settled
    outside.

    Returns ``[{'period_start', 'period_end', 'due_date', 'rent', 'terms'}, ...]``.
    """
    one = timezone.timedelta(days=1)
    on_date = on_date or timezone.localdate()
    if spans is None:
        spans = list(Invoice.objects.filter(tenancy=tenancy).values_list(
            'period_start', 'period_end'))
    spans = sorted(spans)
    first_start = spans[0][0] if spans else None
    last_end = max((end for _start, end in spans), default=None)
    if tenancy.is_team_let:
        begin = first_start or tenancy.billing_period_for(on_date)[0]
    else:
        begin = tenancy.check_in_date
    leaving = min(filter(None, [tenancy.actual_check_out, tenancy.expected_check_out]),
                  default=None)
    owed, first, anchor = [], not spans, begin
    for _n in range(PERIOD_SCAN_LIMIT):
        if len(owed) >= CATCH_UP_LIMIT:
            break
        period_start, period_end = tenancy.period_for(anchor)
        anchor = period_end + one
        if leaving and period_start > leaving:
            break
        if not fill_gaps and spans and first_start <= period_start and period_end <= last_end:
            anchor = max(anchor, last_end + one)       # wholly inside the invoiced run
            continue
        low = max(period_start, tenancy.check_in_date, begin)
        high = min(period_end, leaving) if leaving else period_end
        if high < low:
            continue
        pieces = _uncovered(low, high, spans)
        if not fill_gaps and spans:
            # Only before the first invoice or after the last one.
            pieces = [(a, min(b, first_start - one)) for a, b in pieces if a < first_start] + \
                     [(max(a, last_end + one), b) for a, b in pieces if b > last_end]
        stop = False
        for piece_start, piece_end in pieces:
            due_now = (piece_start <= on_date if tenancy.bills_in_advance
                       else piece_end < on_date)
            if not due_now:
                stop = True
                break
            terms = period_terms(tenancy, period_start, period_end, first_invoice=first)
            if (piece_start, piece_end) != (terms['period_start'], terms['period_end']):
                # Part of a period: those days only, by the day.
                terms = dict(terms, period_start=piece_start, period_end=piece_end,
                             due_date=tenancy.due_date_for(piece_start, piece_end),
                             rent_amount=None, prorata=True)
            owed.append({'period_start': piece_start, 'period_end': piece_end,
                         'due_date': terms['due_date'], 'rent': rent_for_terms(tenancy, terms),
                         'terms': terms})
            first = False
        if stop:
            break
    return owed


def bill_stay_now(tenancy, user=None):
    """Raise the invoices one stay owes by today, before money is taken from it.

    So "collect" always has every month on an invoice: the whole balance is
    there to pay, all at once or in parts. One stay is a few invoices at most
    (``CATCH_UP_LIMIT``), quick enough to do while the page opens.
    """
    created, _finished = generate_due_invoices_within(
        tenancy.tenant, seconds=10, user=user, only=[tenancy.pk])
    return created


def _catch_up(tenancy, on_date, user):
    """Invoice every period ``unbilled_rent`` finds — the missed months too.

    A day the job did not run (no server cron, a holiday) would otherwise lose
    that month for good: the run used to look only at today's period. Each
    missed period gets its invoice with its real due date, from check-in when
    nothing was billed yet, so what the overdue page shows as "not invoiced
    yet" is exactly what this raises.
    """
    return [build_invoice(tenancy, user=user, **row['terms'])
            for row in unbilled_rent(tenancy, on_date)]


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
    provisional = needs_approval(user) and not (mode and mode.settles_from_credit)
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
def record_multi_invoice_payment(*, tenant, user, tenancy, invoices, amount, mode=None,
                                 reference='', paid_on=None, note='', cheque_due_date=None):
    """Record one receipt and allocate it across open invoices for one tenancy."""
    requested_ids = {invoice.pk for invoice in invoices}
    invoices = list(Invoice.objects.select_for_update().filter(
        pk__in=requested_ids, tenant=tenant,
        tenancy=tenancy,
    ).exclude(status__counts_as_settled=True).order_by('due_date', 'period_start', 'pk'))
    if not invoices or len(invoices) != len(requested_ids):
        raise ValueError('Select at least one open invoice for this tenancy.')
    total_balance = sum((invoice.balance for invoice in invoices), Decimal('0'))
    amount = Decimal(amount).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    if amount <= 0 or amount > total_balance:
        raise ValueError('Payment amount exceeds the selected invoice balances.')
    mode = resolve_mode(tenant, mode)
    if mode and mode.settles_from_credit and amount > tenancy.guest.advance_credit():
        raise ValueError('There is not enough advance credit for this payment.')

    payment = record_payment(
        tenant=tenant, user=user, tenancy=tenancy, amount=amount, mode=mode,
        reference=reference, paid_on=paid_on, note=note,
        cheque_due_date=cheque_due_date,
    )
    remaining = amount
    for invoice in invoices:
        allocated = min(remaining, invoice.balance)
        if allocated > 0:
            PaymentAllocation.objects.create(payment=payment, invoice=invoice,
                                             amount=allocated)
            invoice.recalculate()
            remaining -= allocated
        if remaining <= 0:
            break
    return payment


def save_payment(*, tenant, user, data):
    """Record what a valid ``PaymentForm`` holds, the way the payment screen does.

    One invoice or none is a plain receipt; several are allocated oldest
    first. Money held as advance (no invoice) is spent on unpaid invoices at
    once when it needs no approval. Returns ``(payment, credit_applied)``.
    """
    selected = data['invoices']
    common = {
        'tenant': tenant, 'user': user, 'tenancy': data['tenancy'],
        'amount': data['amount'], 'mode': data['mode'],
        'reference': data.get('reference') or '', 'paid_on': data.get('paid_on'),
        'note': data.get('note') or '', 'cheque_due_date': data.get('cheque_due_date'),
    }
    applied = False
    if len(selected) > 1:
        payment = record_multi_invoice_payment(**common, invoices=selected)
    else:
        payment = record_payment(**common, invoice=selected[0] if selected else None)
        if not selected and not payment.from_credit and not payment.awaits_approval:
            applied = bool(apply_guest_credit(guest=data['tenancy'].guest, user=user))
    return payment, applied


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
    if payment.invoice_id is None and not payment.from_credit and not payment.is_reversed \
            and not payment.awaits_approval:
        if payment.tenancy.guest.advance_credit() - payment.amount < 0:
            raise ValueError(_('Apply reversals to the credit-funded invoices first.'))
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
    if payment.invoice_id is None and not payment.allocations.exists() \
            and not payment.is_reversed:
        credit = payment.tenancy.guest.advance_credit()
        if credit - payment.amount < 0:
            return False, _('Part of this advance has already been used as credit. '
                            'Delete those credit payments first.')
    invoice_ids = set(payment.allocations.values_list('invoice_id', flat=True))
    if payment.invoice_id:
        invoice_ids.add(payment.invoice_id)
    with transaction.atomic():
        audit.log(audit.AuditLog.ACTION_DELETE, payment, tenant=payment.tenant,
                  summary=_('Deleted receipt %(no)s (%(amount)s)') % {
                      'no': payment.receipt_number, 'amount': payment.amount})
        LedgerEntry.objects.filter(
            tenant=payment.tenant, source_type=LedgerEntry.SOURCE_PAYMENT,
            source_id=str(payment.pk),
        ).delete()
        payment.delete()
        for invoice in Invoice.objects.filter(pk__in=invoice_ids):
            invoice.recalculate()
    return True, ''


def delete_invoice(*, invoice, user):
    """Admin-only hard delete. Returns ``(done, reason)``."""
    if invoice.payments.exists() or invoice.payment_allocations.exists():
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


@transaction.atomic
def apply_guest_credit(*, guest, user):
    """Use available advance credit against the guest's oldest open rent first."""
    invoices = Invoice.objects.filter(tenancy__guest=guest).exclude(
        status__counts_as_settled=True).order_by('due_date', 'period_start', 'pk')
    applied = []
    for invoice in invoices:
        if guest.advance_credit() <= 0:
            break
        payment = settle_from_credit(invoice=invoice, user=user)
        if payment:
            applied.append(payment)
    return applied


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


def aging_band(due_date, today=None):
    """The aging bucket an amount falls in: 'current', '0-30', '31-60' or '60+'."""
    today = today or timezone.localdate()
    late = (today - due_date).days
    if late <= 0:
        return 'current'
    return '0-30' if late <= 30 else '31-60' if late <= 60 else '60+'


def unbilled_for(tenancies, on_date=None):
    """``unbilled_rent`` for many stays: ``[{..., 'tenancy'}, ...]``, oldest first."""
    on_date = on_date or timezone.localdate()
    rows = []
    for tenancy in tenancies:
        rows += [dict(row, tenancy=tenancy) for row in unbilled_rent(tenancy, on_date)]
    return sorted(rows, key=lambda row: (row['due_date'], row['tenancy'].pk))


def dues_by_guest(invoices, unbilled, today=None):
    """Every pending month per stay, invoiced or not yet, with the total and lateness.

    ``invoices`` are unpaid invoices, ``unbilled`` rows from ``unbilled_for``.
    Returns one dict per stay — ``tenancy, months, total, not_invoiced, oldest,
    days_late`` — the latest first, then the largest.
    """
    today = today or timezone.localdate()
    stays = {}

    def entry(tenancy):
        return stays.setdefault(tenancy.pk, {
            'tenancy': tenancy, 'months': [], 'total': Decimal('0'),
            'not_invoiced': Decimal('0'), 'oldest': None})

    for invoice in invoices:
        row = entry(invoice.tenancy)
        row['months'].append({'start': invoice.period_start, 'end': invoice.period_end,
                              'amount': invoice.balance, 'due': invoice.due_date,
                              'invoice': invoice})
        row['total'] += invoice.balance
    for item in unbilled:
        row = entry(item['tenancy'])
        row['months'].append({'start': item['period_start'], 'end': item['period_end'],
                              'amount': item['rent'], 'due': item['due_date'], 'invoice': None})
        row['total'] += item['rent']
        row['not_invoiced'] += item['rent']
    for row in stays.values():
        row['months'].sort(key=lambda month: month['start'])
        row['oldest'] = min(month['due'] for month in row['months'])
        row['days_late'] = max((today - row['oldest']).days, 0)
    return sorted(stays.values(), key=lambda row: (-row['days_late'], -row['total']))


def outstanding_ledger_check(tenant):
    """Guard used by reports: income posted vs payments recorded."""
    payments = Payment.objects.filter(tenant=tenant, is_reversed=False, from_credit=False)
    posted = LedgerEntry.objects.filter(
        tenant=tenant, source_type=LedgerEntry.SOURCE_PAYMENT
    ).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    taken = payments.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    return {'posted': posted, 'collected': taken, 'difference': taken - posted}


# --- Moving existing stays onto their check-in day ---------------------------
def plan_bill_day(tenancy):
    """What moving a stay onto its check-in day would do, or ``None``.

    New stays are billed on their check-in day (``bill_day_for``). A stay
    from before keeps its old day until moved. Moving starts at its next
    unbilled rent: when that falls inside a period of the new cycle, one
    short invoice covers the days up to it, charged by the day, then the
    full rent follows on the new day. Invoices already raised stay as they
    are. Team lets and weekly or daily stays keep their own rent day.

    Returns ``{'day', 'start', 'end', 'rent', 'due'}``; ``start`` is ``None``
    when no short invoice is needed.
    """
    from django.db.models import Max

    from guests.services import bill_day_for

    if (tenancy.billing_cycle != Tenancy.CYCLE_MONTHLY or tenancy.is_team_let
            or not tenancy.is_live):
        return None
    day = bill_day_for(tenancy.check_in_date)
    if tenancy.billing_day == day:
        return None
    plan = {'day': day, 'start': None, 'end': None, 'rent': None, 'due': None}
    last_end = Invoice.objects.filter(tenancy=tenancy).aggregate(last=Max('period_end'))['last']
    if last_end is None:
        return plan
    start = last_end + timezone.timedelta(days=1)
    leaving = tenancy.actual_check_out or tenancy.expected_check_out
    if leaving and leaving < start:
        return None                     # billed to the end already: nothing to move
    kept = tenancy.billing_day
    tenancy.billing_day = day
    try:
        period_start, period_end = tenancy.period_for(start)
        length = tenancy.period_length(period_start)
    finally:
        tenancy.billing_day = kept
    if period_start == start:
        return plan
    end = min(period_end, leaving) if leaving else period_end
    plan.update(start=start, end=end, due=tenancy.due_date_for(start, end),
                rent=tenancy.prorata((end - start).days + 1, period_days=length))
    return plan


@transaction.atomic
def move_to_check_in_day(*, tenancy, user):
    """Apply ``plan_bill_day``: the new bill day, and the short invoice if any.

    Returns ``(plan, invoice_or_None)``, or ``None`` when there was nothing to do.
    """
    plan = plan_bill_day(tenancy)
    if plan is None:
        return None
    before = audit.snapshot(tenancy, ['billing_day'])
    tenancy.billing_day = plan['day']
    tenancy.save(update_fields=['billing_day', 'updated_at'])
    audit.log(audit.AuditLog.ACTION_UPDATE, tenancy, tenant=tenancy.tenant,
              summary=_('Rent of %(guest)s moved to day %(day)s, the check-in day') % {
                  'guest': tenancy.guest.full_name, 'day': plan['day']},
              changes=audit.diff(before, audit.snapshot(tenancy, ['billing_day'])))
    invoice = None
    if plan['start']:
        invoice = build_invoice(
            tenancy, user=user, period_start=plan['start'], period_end=plan['end'],
            due_date=plan['due'], rent_amount=plan['rent'], prorata=True,
            notes=_('Short period: rent moves to day %(day)s, the check-in day') % {
                'day': plan['day']})
        apply_guest_credit(guest=tenancy.guest, user=user)
    return plan, invoice


def stays_to_move(tenant, tenancies=None):
    """``[(tenancy, plan), ...]`` for every live stay not yet on its check-in day."""
    qs = tenancies if tenancies is not None else Tenancy.objects.filter(tenant=tenant)
    qs = qs.filter(status__code__in=Tenancy.LIVE_STATUS_CODES).select_related(
        'guest', 'building', 'floor', 'apartment', 'room', 'bed').order_by('guest__full_name')
    rows = []
    for tenancy in qs:
        plan = plan_bill_day(tenancy)
        if plan is not None:
            rows.append((tenancy, plan))
    return rows


def move_stays_within(tenant, *, user, seconds=20, tenancies=None):
    """Move stays onto their check-in day until ``seconds`` run out: ``(moved, finished)``.

    Each stay is moved in its own transaction, so stopping early loses nothing.
    """
    import time

    deadline = time.monotonic() + seconds
    moved = 0
    for tenancy, _plan in stays_to_move(tenant, tenancies):
        if time.monotonic() > deadline:
            return moved, False
        if move_to_check_in_day(tenancy=tenancy, user=user):
            moved += 1
    return moved, True
