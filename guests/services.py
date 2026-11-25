"""Check-in, check-out, bed transfer and deposit settlement."""
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import (
    Case, DecimalField, F, OuterRef, Q, Subquery, Sum, Value, When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext as _

from core import audit
from core.models import Status
from properties.models import Room

from .models import BedTransfer, DepositTransaction, Tenancy


@transaction.atomic
def check_in(*, tenant, user, guest, room, bed, check_in_date, monthly_rent,
             deposit_amount=Decimal('0'), deposit_received=Decimal('0'),
             billing_day=1, billing_cycle=Tenancy.CYCLE_MONTHLY,
             expected_check_out=None, agreement_note='',
             rent_mode=Tenancy.MODE_ADVANCE, prorate_first_period=True,
             first_invoice_amount=None):
    """Put a guest into a bed (or a whole room / flat) and occupy it."""
    tenancy = Tenancy(
        tenant=tenant,
        guest=guest,
        room=room,
        bed=bed,
        check_in_date=check_in_date,
        expected_check_out=expected_check_out,
        monthly_rent=monthly_rent,
        billing_cycle=billing_cycle,
        billing_day=billing_day,
        deposit_amount=deposit_amount,
        rent_mode=rent_mode,
        prorate_first_period=prorate_first_period,
        first_invoice_amount=first_invoice_amount,
        agreement_note=agreement_note,
        created_by=user,
    )
    tenancy.full_clean(exclude=['floor', 'building', 'status'])
    tenancy.save()

    _occupy(tenancy)

    if deposit_received:
        record_deposit(tenancy=tenancy, user=user, amount=deposit_received,
                       reason=_('Deposit collected at check-in'))

    audit.log(audit.AuditLog.ACTION_CREATE, tenancy, tenant=tenant,
              summary=_('Checked in %(guest)s to %(loc)s at %(rent)s/month') % {
                  'guest': guest.full_name, 'loc': tenancy.location_label(),
                  'rent': monthly_rent})
    return tenancy


def raise_first_invoice(*, tenancy, user):
    """The invoice for a new stay's first period, with any advance credit applied."""
    from billing.services import apply_guest_credit, build_invoice

    period_start, period_end = tenancy.period_for(tenancy.check_in_date)
    start = max(period_start, tenancy.check_in_date)
    # Only split the month when the agreement says to; plenty of owners take the
    # whole month's rent however late in the month the guest arrived.
    prorata = start > period_start and tenancy.prorate_first_period
    invoice = build_invoice(
        tenancy, period_start=start, period_end=period_end,
        due_date=tenancy.due_date_for(start, period_end), user=user,
        rent_amount=tenancy.first_invoice_amount, prorata=prorata,
        period_days=tenancy.period_length(period_start),
    )
    apply_guest_credit(guest=tenancy.guest, user=user)
    return invoice


def check_in_date_problem(tenancy, new_date):
    """Why a stay cannot start on ``new_date``, or ``None`` when it can.

    The stay may not start after it ended or after a bed transfer, and the
    bed (or the room let whole) must have been free from that day: the guest
    before may not still have been in it.
    """
    from datetime import date as date_type

    if tenancy.actual_check_out and new_date > tenancy.actual_check_out:
        return _('The stay ended on %(date)s, so it cannot start after that.') % {
            'date': tenancy.actual_check_out.strftime('%d %b %Y')}
    if tenancy.expected_check_out and new_date > tenancy.expected_check_out:
        return _('The guest is leaving on %(date)s, so the stay cannot start after that.') % {
            'date': tenancy.expected_check_out.strftime('%d %b %Y')}
    moved = tenancy.transfers.filter(transfer_date__lt=new_date).order_by('transfer_date').first()
    if moved:
        return _('The guest moved beds on %(date)s, so the stay must start before that.') % {
            'date': moved.transfer_date.strftime('%d %b %Y')}
    ends = tenancy.actual_check_out or date_type.max
    others = Tenancy.objects.filter(tenant=tenancy.tenant).exclude(pk=tenancy.pk)
    if tenancy.bed_id:
        others = others.filter(bed=tenancy.bed)
    elif tenancy.room_id:
        others = others.filter(room=tenancy.room, bed__isnull=True)
    else:
        return None
    for other in others.select_related('guest').filter(check_in_date__lt=ends):
        other_ends = other.actual_check_out or date_type.max
        # Leaving and arriving on the same day is a normal change-over.
        if other_ends > new_date:
            return _('%(name)s was in this bed until %(date)s.') % {
                'name': other.guest.full_name,
                'date': other_ends.strftime('%d %b %Y') if other.actual_check_out
                else _('now')}
    return None


def bill_day_for(check_in_date):
    """The day of the month a stay is billed on: its check-in day.

    Checked in on the 25th, billed every 25th. The 29th to the 31st do not
    come every month, so those stays are billed on the 28th.
    """
    return min(check_in_date.day, 28)


def _periods(tenancy, billing_day, on_date):
    """The billing period around ``on_date`` if the stay were billed on ``billing_day``."""
    kept = tenancy.billing_day
    tenancy.billing_day = billing_day
    try:
        period = tenancy.period_for(on_date)
        length = tenancy.period_length(period[0])
    finally:
        tenancy.billing_day = kept
    return period, length


def plan_first_invoice(tenancy, old_date, new_date):
    """What moving the check-in date does to the first invoice, without doing it.

    A stay billed on its check-in day keeps that rule: moved from the 25th to
    the 18th, it is billed on the 18th, and its first invoice runs from the
    18th for a full period. The invoice is only changed while that is safe:
    nothing is paid on it, and (when the bill day moves) no later invoice was
    raised on the old day. Otherwise ``rent`` is ``None`` and ``reason`` says
    why. Returns a dict: ``invoice, start, end, rent, billing_day, reason``.
    """
    from billing.models import Invoice, Payment, PaymentAllocation

    follows = (tenancy.billing_cycle == Tenancy.CYCLE_MONTHLY
               and tenancy.billing_day == bill_day_for(old_date))
    billing_day = bill_day_for(new_date) if follows else tenancy.billing_day
    plan = {'invoice': None, 'start': None, 'end': None, 'rent': None,
            'billing_day': tenancy.billing_day, 'reason': ''}
    invoices = Invoice.objects.filter(tenancy=tenancy).exclude(status__code='cancelled')
    invoice = (invoices.filter(period_start=old_date).order_by('pk').first()
               or invoices.filter(period_start__lte=old_date, period_end__gte=old_date)
               .order_by('pk').first())
    if invoice is None:
        # Nothing billed yet: the bill day can simply follow the date.
        plan['billing_day'] = billing_day
        return plan
    plan['invoice'] = invoice
    paid = (invoice.paid_amount > 0
            or Payment.objects.filter(invoice=invoice, is_reversed=False).exists()
            or PaymentAllocation.objects.filter(invoice=invoice).exists())
    if paid:
        plan['reason'] = _(
            'Invoice %(no)s already has money paid on it, so it was not changed. Correct it on '
            'the invoice page if the rent should change.') % {'no': invoice.number}
        return plan
    if billing_day != tenancy.billing_day and invoices.filter(
            period_start__gt=invoice.period_start).exists():
        plan['reason'] = _(
            'Later invoices were raised on day %(day)s, so invoice %(no)s and the bill day were '
            'not changed. Check the invoices on the stay page.') % {
            'day': tenancy.billing_day, 'no': invoice.number}
        return plan
    (period_start, period_end), length = _periods(tenancy, billing_day, new_date)
    if billing_day == tenancy.billing_day and (period_start, period_end) != tenancy.period_for(
            old_date):
        plan['reason'] = _(
            'The new date is in another billing period, so invoice %(no)s was not changed. '
            'Check the invoices on the stay page.') % {'no': invoice.number}
        return plan
    start = max(period_start, new_date)
    prorata = start > period_start and tenancy.prorate_first_period
    if tenancy.first_invoice_amount is not None:
        rent = tenancy.first_invoice_amount
    elif prorata:
        rent = tenancy.prorata((period_end - start).days + 1, period_days=length)
    else:
        rent = tenancy.monthly_rent
    plan.update(start=start, end=period_end, billing_day=billing_day, prorata=prorata,
                rent=Decimal(rent).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    return plan


@transaction.atomic
def change_check_in_date(*, tenancy, user, new_date, rebill=True):
    """Move a stay's check-in date, and its first invoice with it when that is safe.

    ``rebill`` false (someone without billing rights) leaves every invoice
    alone. Returns a note for the user about the invoice, or ``''``.
    Raises ``ValidationError`` when the stay cannot start on that day.
    """
    old_date = tenancy.check_in_date
    if new_date == old_date:
        return ''
    problem = check_in_date_problem(tenancy, new_date)
    if problem:
        raise ValidationError({'check_in_date': problem})
    before = audit.snapshot(tenancy, ['check_in_date'])
    tenancy.check_in_date = new_date
    tenancy.full_clean(exclude=['floor', 'building', 'status'])
    tenancy.save()
    audit.log(audit.AuditLog.ACTION_UPDATE, tenancy, tenant=tenancy.tenant,
              summary=_('Check-in date of %(guest)s changed from %(old)s to %(new)s') % {
                  'guest': tenancy.guest.full_name, 'old': old_date.strftime('%d %b %Y'),
                  'new': new_date.strftime('%d %b %Y')},
              changes=audit.diff(before, audit.snapshot(tenancy, ['check_in_date'])))
    return rebill_first_invoice(tenancy=tenancy, user=user, old_date=old_date,
                                rebill=rebill)


def rebill_first_invoice(*, tenancy, user, old_date, rebill=True):
    """After the check-in date moved: move the first invoice (and the bill day) with it.

    Returns a note for the user, or ``''`` when there was nothing to say.
    """
    plan = plan_first_invoice(tenancy, old_date, tenancy.check_in_date)
    invoice = plan['invoice']
    if not rebill:
        if invoice is None and plan['billing_day'] == tenancy.billing_day:
            return ''
        return _('Invoices and the bill day were not changed: that needs billing rights.')
    notes = []
    if plan['billing_day'] != tenancy.billing_day and (invoice is None or plan['rent'] is not None):
        before = audit.snapshot(tenancy, ['billing_day'])
        tenancy.billing_day = plan['billing_day']
        tenancy.save(update_fields=['billing_day', 'updated_at'])
        audit.log(audit.AuditLog.ACTION_UPDATE, tenancy, tenant=tenancy.tenant,
                  summary=_('Bill day of %(guest)s moved to day %(day)s with the check-in '
                            'date') % {'guest': tenancy.guest.full_name, 'day': tenancy.billing_day},
                  changes=audit.diff(before, audit.snapshot(tenancy, ['billing_day'])))
        notes.append(_('Rent is now billed on day %(day)s of each month.') % {
            'day': tenancy.billing_day})
    if invoice is None:
        return ' '.join(notes)
    if plan['rent'] is None:
        return plan['reason']
    tracked = ['period_start', 'period_end', 'rent_amount', 'total', 'due_date']
    before = audit.snapshot(invoice, tracked)
    invoice.period_start = plan['start']
    invoice.period_end = plan['end']
    invoice.rent_amount = plan['rent']
    invoice.is_prorata = plan['prorata']
    invoice.due_date = tenancy.due_date_for(plan['start'], plan['end'])
    invoice.save(update_fields=['period_start', 'period_end', 'rent_amount', 'is_prorata',
                                'due_date', 'updated_at'])
    invoice.recalculate()
    audit.log(audit.AuditLog.ACTION_UPDATE, invoice, tenant=tenancy.tenant,
              summary=_('Invoice %(no)s moved with the check-in date of %(guest)s') % {
                  'no': invoice.number, 'guest': tenancy.guest.full_name},
              changes=audit.diff(before, audit.snapshot(invoice, tracked)))
    notes.insert(0, _('Invoice %(no)s now runs from %(start)s to %(end)s: %(amount)s.') % {
        'no': invoice.number, 'start': plan['start'].strftime('%d %b %Y'),
        'end': plan['end'].strftime('%d %b %Y'), 'amount': invoice.total})
    return ' '.join(notes)


@transaction.atomic
def check_out_and_settle(*, tenant, user, tenancy, data, can_bill):
    """Check-out as the check-out form asks for it: final rent, then the stay.

    ``data`` is a valid ``CheckOutForm``'s ``cleaned_data``. Returns the
    payment taken for the final rent, if any.
    """
    from billing import services as billing

    deductions = []
    if data.get('deduction_amount'):
        deductions.append({'amount': data['deduction_amount'],
                           'reason': data['deduction_reason']})
    payment = None
    if can_bill:
        _invoice, payment = billing.settle_final_period(
            tenant=tenant, user=user, tenancy=tenancy,
            leaving_on=data['check_out_date'], rent_amount=data.get('final_rent'),
            collect_amount=data.get('collect_amount'), mode=data.get('collect_mode'),
            reference=data.get('collect_reference') or '',
            note=_('Final rent on check-out'),
        )
    check_out(
        tenancy=tenancy, user=user, check_out_date=data['check_out_date'],
        deductions=deductions, refund_amount=data.get('refund_amount') or Decimal('0'),
        note=data.get('note', ''),
    )
    return payment


@transaction.atomic
def check_out(*, tenancy, user, check_out_date=None, deductions=None, refund_amount=Decimal('0'),
              note=''):
    """Close a stay, settle the deposit and release the bed."""
    check_out_date = check_out_date or timezone.localdate()
    before = audit.snapshot(tenancy, ['status', 'actual_check_out'])

    for item in (deductions or []):
        amount = Decimal(item.get('amount') or 0)
        if amount <= 0:
            continue
        deduction = DepositTransaction.objects.create(
            tenant=tenancy.tenant, tenancy=tenancy, kind=DepositTransaction.KIND_DEDUCT,
            amount=amount, reason=item.get('reason', ''), happened_on=check_out_date,
            recorded_by=user,
        )
        post_deduction_as_income(deduction)

    if refund_amount and refund_amount > 0:
        DepositTransaction.objects.create(
            tenant=tenancy.tenant, tenancy=tenancy, kind=DepositTransaction.KIND_REFUND,
            amount=refund_amount, reason=note or _('Deposit refunded on check-out'),
            happened_on=check_out_date, recorded_by=user,
        )

    tenancy.actual_check_out = check_out_date
    tenancy.checked_out_by = user
    tenancy.status = Status.by_code(tenancy.tenant, Status.KIND_TENANCY, 'vacated') or tenancy.status
    tenancy.save()

    _release(tenancy)

    audit.log(audit.AuditLog.ACTION_UPDATE, tenancy, tenant=tenancy.tenant,
              summary=_('Checked out %(guest)s on %(date)s') % {
                  'guest': tenancy.guest.full_name, 'date': check_out_date},
              changes=audit.diff(before, audit.snapshot(tenancy, ['status', 'actual_check_out'])))
    return tenancy


@transaction.atomic
def give_notice(*, tenancy, user, notice_date=None, expected_check_out=None):
    tenancy.notice_given_on = notice_date or timezone.localdate()
    if expected_check_out:
        tenancy.expected_check_out = expected_check_out
    tenancy.status = Status.by_code(tenancy.tenant, Status.KIND_TENANCY, 'notice') or tenancy.status
    tenancy.save()
    audit.log(audit.AuditLog.ACTION_UPDATE, tenancy, tenant=tenancy.tenant,
              summary=_('Notice recorded, leaving on %(date)s') % {
                  'date': tenancy.expected_check_out or '—'})
    return tenancy


@transaction.atomic
def transfer_bed(*, tenancy, user, new_bed=None, new_room=None, transfer_date=None,
                 new_rent=None, reason=''):
    """Move a guest mid-month and split the rent pro-rata across both beds."""
    transfer_date = transfer_date or timezone.localdate()
    new_room = new_room or (new_bed.room if new_bed else tenancy.room)
    old_bed, old_room = tenancy.bed, tenancy.room
    old_rent = tenancy.monthly_rent
    if new_rent is None:
        if new_bed and new_room.rent_basis == Room.RENT_PER_BED:
            new_rent = new_bed.monthly_price or old_rent
        else:
            new_rent = new_room.monthly_rent or old_rent

    period_start, period_end = tenancy.period_for(transfer_date)
    period_days = (period_end - period_start).days + 1
    old_days = max((transfer_date - period_start).days, 0)
    new_days = max((period_end - transfer_date).days + 1, 0)

    def slice_of(rent, days):
        if not period_days:
            return Decimal('0.00')
        return (Decimal(rent) * Decimal(days) / Decimal(period_days)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)

    old_prorata = slice_of(old_rent, old_days)
    if old_rent == new_rent:
        # Same rent either side: the two slices must total exactly one month, so
        # the rounding remainder goes to the second one rather than inventing rent.
        new_prorata = (Decimal(new_rent) - old_prorata).quantize(Decimal('0.01'))
    else:
        new_prorata = slice_of(new_rent, new_days)

    _release(tenancy)

    tenancy.bed = new_bed
    tenancy.room = new_room
    tenancy.monthly_rent = new_rent
    tenancy.full_clean(exclude=['floor', 'building', 'status'])
    tenancy.save()
    _occupy(tenancy)

    record = BedTransfer.objects.create(
        tenant=tenancy.tenant, tenancy=tenancy,
        from_bed=old_bed, to_bed=new_bed, from_room=old_room, to_room=new_room,
        transfer_date=transfer_date,
        old_rent=old_rent, new_rent=new_rent,
        old_days=old_days, new_days=new_days,
        old_prorata=old_prorata, new_prorata=new_prorata,
        reason=reason, moved_by=user,
    )
    audit.log(audit.AuditLog.ACTION_UPDATE, tenancy, tenant=tenancy.tenant,
              summary=_('Transferred %(guest)s from %(old)s to %(new)s') % {
                  'guest': tenancy.guest.full_name, 'old': old_bed or old_room,
                  'new': new_bed or new_room},
              changes={'rent': [str(old_rent), str(new_rent)],
                       'prorata': [str(record.old_prorata), str(record.new_prorata)]})
    return record


def record_deposit(*, tenancy, user, amount, reason='', receipt_number='', mode=None,
                   reference=''):
    from billing.services import needs_approval, resolve_mode

    mode = resolve_mode(tenancy.tenant, mode)
    entry = DepositTransaction.objects.create(
        tenant=tenancy.tenant, tenancy=tenancy, kind=DepositTransaction.KIND_HOLD,
        amount=amount, reason=reason, recorded_by=user, receipt_number=receipt_number,
        mode=mode, reference=reference,
        approval='pending' if needs_approval(user) else 'not_required',
    )
    audit.log(audit.AuditLog.ACTION_CREATE, entry, tenant=tenancy.tenant,
              summary=_('Deposit %(no)s — %(amount)s held for %(guest)s') % {
                  'no': entry.receipt_number, 'amount': amount,
                  'guest': tenancy.guest.full_name})
    return entry


@transaction.atomic
def approve_deposit(*, entry, user, note=''):
    entry.approval = 'approved'
    entry.approved_by = user
    entry.approved_at = timezone.now()
    entry.approval_note = note
    entry.save(update_fields=['approval', 'approved_by', 'approved_at', 'approval_note',
                              'updated_at'])
    audit.log(audit.AuditLog.ACTION_UPDATE, entry, tenant=entry.tenant,
              summary=_('Approved deposit %(no)s taken by %(who)s') % {
                  'no': entry.receipt_number,
                  'who': entry.recorded_by.display_name() if entry.recorded_by_id else '—'})
    return entry


@transaction.atomic
def reject_deposit(*, entry, user, reason):
    """Rejecting means the money never arrived, so the entry goes."""
    from accounting.models import LedgerEntry

    label = entry.receipt_number
    taker = entry.recorded_by.display_name() if entry.recorded_by_id else '—'
    LedgerEntry.objects.filter(
        tenant=entry.tenant, source_type=LedgerEntry.SOURCE_DEPOSIT,
        source_id=str(entry.pk)).delete()
    audit.log(audit.AuditLog.ACTION_REVERSE, entry, tenant=entry.tenant,
              summary=_('Rejected deposit %(no)s taken by %(who)s: %(reason)s') % {
                  'no': label, 'who': taker, 'reason': reason},
              changes={'amount': [str(entry.amount), None], 'reason': [reason, None]})
    entry.delete()
    return label


def post_deduction_as_income(entry):
    """A kept deposit is income; a held or refunded one is not.

    Holding a deposit creates a liability, not a profit, so it stays out of the
    books until the building actually keeps some of it.
    """
    from accounting.models import KIND_INCOME, Category, LedgerEntry

    if entry.kind != DepositTransaction.KIND_DEDUCT:
        return None
    category = (
        Category.by_code(entry.tenant, KIND_INCOME, 'security_deposit')
        or Category.objects.filter(tenant=entry.tenant, kind=KIND_INCOME).first()
    )
    if category is None:
        return None
    return LedgerEntry.objects.update_or_create(
        tenant=entry.tenant, source_type=LedgerEntry.SOURCE_DEPOSIT,
        source_id=str(entry.pk),
        defaults={
            'kind': KIND_INCOME,
            'category': category,
            'amount': entry.amount,
            'entry_date': entry.happened_on,
            'description': _('Deposit kept — %(reason)s') % {
                'reason': entry.reason or entry.tenancy.guest.full_name},
            'building': entry.tenancy.building,
            'floor': entry.tenancy.floor,
            'room': entry.tenancy.room,
            'reference': entry.receipt_number,
            'recorded_by': entry.recorded_by,
        },
    )[0]


def deposit_totals(tenant, start=None, end=None, building=None, rows=None):
    """Deposit money in and out. ``net`` is what the building is holding.

    Deposits are the guests' money, so this is deliberately separate from the
    income and expense ledger — counting held deposits as profit would overstate
    what the building has actually earned. ``rows`` narrows the base set (a
    staff member's floors, a place picked on the report).
    """
    rows = rows if rows is not None else DepositTransaction.objects.filter(tenant=tenant)
    if start and end:
        rows = rows.filter(happened_on__range=(start, end))
    if building is not None:
        rows = rows.filter(tenancy__building=building)

    totals = {row['kind']: row['total'] or Decimal('0')
              for row in rows.order_by().values('kind').annotate(total=Sum('amount'))}
    received = totals.get(DepositTransaction.KIND_HOLD, Decimal('0'))
    deducted = totals.get(DepositTransaction.KIND_DEDUCT, Decimal('0'))
    refunded = totals.get(DepositTransaction.KIND_REFUND, Decimal('0'))
    return {
        'received': received,
        'deducted': deducted,
        'refunded': refunded,
        'paid_out': deducted + refunded,
        'net': received - deducted - refunded,
        'count': rows.count(),
        'rows': rows,
    }


def deposits_held(tenant):
    """Everything still in hand, across every tenancy past and present."""
    return deposit_totals(tenant)['net']


def guest_footprint(guest):
    """Everything that would go with this guest, for the confirmation screen."""
    from billing.models import Invoice, Payment
    from messaging.models import MessageLog

    payments = Payment.objects.filter(tenancy__guest=guest, is_reversed=False)
    tenancies = Tenancy.objects.filter(guest=guest)
    return {
        'tenancies': tenancies.count(),
        'live_tenancy': tenancies.filter(
            status__code__in=Tenancy.LIVE_STATUS_CODES).select_related(
                'bed', 'room', 'building').first(),
        'invoices': Invoice.objects.filter(tenancy__guest=guest).count(),
        'payments': payments.count(),
        'collected': payments.aggregate(total=Sum('amount'))['total'] or Decimal('0'),
        'outstanding': guest.outstanding_balance(),
        'deposit_held': sum((t.deposit_held() for t in tenancies), Decimal('0')),
        'documents': guest.documents.count(),
        'messages': MessageLog.objects.filter(guest=guest).count(),
    }


@transaction.atomic
def purge_guest(*, guest, user):
    """Erase a guest and everything attached to them. There is no undo.

    Deletion order matters — tenancies, invoices and payments protect the rows
    they point at. The income those payments posted goes too, otherwise the
    ledger keeps money with no source behind it. What survives is one audit
    entry recording who did it and what the totals were.
    """
    from accounting.models import LedgerEntry
    from billing.models import Invoice, InvoiceLine, Payment
    from messaging.models import MessageLog

    tenant = guest.tenant
    footprint = guest_footprint(guest)

    # Release any bed they are still in first, or it stays marked occupied with
    # nobody in it once the tenancy is gone.
    for tenancy in Tenancy.objects.filter(
        guest=guest, status__code__in=Tenancy.LIVE_STATUS_CODES
    ).select_related('bed', 'room'):
        _release(tenancy)

    payment_ids = [str(pk) for pk in
                   Payment.objects.filter(tenancy__guest=guest).values_list('pk', flat=True)]

    removed = {}

    def wipe(label, queryset):
        count = queryset.count()
        if count:
            queryset.delete()
            removed[label] = count

    if payment_ids:
        wipe('ledger entries', LedgerEntry.objects.filter(
            tenant=tenant, source_type=LedgerEntry.SOURCE_PAYMENT,
            source_id__in=payment_ids))
    wipe('messages', MessageLog.objects.filter(guest=guest))
    wipe('payments', Payment.objects.filter(tenancy__guest=guest))
    wipe('invoice lines', InvoiceLine.objects.filter(invoice__tenancy__guest=guest))
    wipe('invoices', Invoice.objects.filter(tenancy__guest=guest))
    wipe('deposit movements', DepositTransaction.objects.filter(tenancy__guest=guest))
    wipe('bed transfers', BedTransfer.objects.filter(tenancy__guest=guest))
    wipe('tenancies', Tenancy.objects.filter(guest=guest))
    wipe('documents', guest.documents.all())

    label = f'{guest.full_name} ({guest.mobile})'
    audit.log(
        audit.AuditLog.ACTION_DELETE, None, tenant=tenant, actor=user,
        model_name='Guest', object_label=label,
        summary=_('Permanently deleted guest %(name)s — %(detail)s') % {
            'name': guest.full_name,
            'detail': ', '.join(f'{count} {what}' for what, count in removed.items())
                      or _('no linked records'),
        },
        changes={
            'guest': [label, None],
            'removed': removed,
            'collected_before_deletion': f"{footprint['collected']:.2f}",
            'outstanding_at_deletion': f"{footprint['outstanding']:.2f}",
        },
    )
    guest.delete()
    return removed


def _beds_of(tenancy):
    """The beds a stay holds: its bed, its whole room, or a team's whole flat."""
    from properties.models import Bed

    if tenancy.bed_id:
        return [tenancy.bed]
    if tenancy.room_id:
        return list(tenancy.room.beds.filter(is_archived=False))
    return list(Bed.objects.filter(room__apartment_id=tenancy.apartment_id, is_archived=False))


def _occupy(tenancy):
    for bed in _beds_of(tenancy):
        bed.set_status_code('occupied')


def _release(tenancy):
    for bed in _beds_of(tenancy):
        still_occupied = Tenancy.objects.filter(
            status__code__in=Tenancy.LIVE_STATUS_CODES,
        ).exclude(pk=tenancy.pk).filter(
            Q(bed_id=bed.pk)
            | Q(room_id=bed.room_id, bed__isnull=True)
            | Q(apartment_id=bed.room.apartment_id, room__isnull=True)
        ).exists()
        if still_occupied:
            continue
        bed.set_status_code('available')


def team_lets_in(apartment_ids):
    """Live team lets for these apartments, as ``{apartment_id: [TeamLet, ...]}``."""
    from teams.models import TeamLet

    grouped = {}
    for team in TeamLet.objects.live().filter(apartment_id__in=apartment_ids).select_related(
            'guest').order_by('team_name'):
        grouped.setdefault(team.apartment_id, []).append(team)
    return grouped


def team_let_blocking(room=None, apartment=None):
    """The live team let holding this room or its whole flat, if any."""
    from teams.models import TeamLet

    apartment_id = apartment.pk if apartment is not None else (
        room.apartment_id if room is not None else None)
    if apartment_id is None:
        return None
    teams = TeamLet.objects.live().filter(apartment_id=apartment_id)
    if room is not None:
        teams = teams.filter(Q(room__isnull=True) | Q(room=room))
    else:
        teams = teams.filter(room__isnull=True)
    return teams.first()


# --- Money per tenancy, for whole lists in one query -------------------------
MONEY = DecimalField(max_digits=14, decimal_places=2)


def _per_tenancy(rows, amount):
    """One summed figure per tenancy, as a correlated subquery."""
    return Coalesce(
        Subquery(rows.filter(tenancy=OuterRef('pk')).order_by().values('tenancy')
                 .annotate(figure=Sum(amount, output_field=MONEY)).values('figure'),
                 output_field=MONEY),
        Value(Decimal('0')), output_field=MONEY)


def with_outstanding(tenancies):
    """Adds ``outstanding_total`` — ``Tenancy.outstanding()`` without a query per row."""
    from billing.models import Invoice

    unsettled = Invoice.objects.exclude(status__counts_as_settled=True)
    return tenancies.annotate(
        outstanding_total=_per_tenancy(unsettled, F('total') - F('paid_amount')))


def with_deposit_held(tenancies):
    """Adds ``deposit_held_total`` — ``Tenancy.deposit_held()`` without a query per row."""
    signed = Case(
        When(kind=DepositTransaction.KIND_HOLD, then=F('amount')),
        When(kind__in=[DepositTransaction.KIND_DEDUCT, DepositTransaction.KIND_REFUND],
             then=-F('amount')),
        default=Value(Decimal('0')), output_field=MONEY)
    return tenancies.annotate(
        deposit_held_total=_per_tenancy(DepositTransaction.objects.all(), signed))
