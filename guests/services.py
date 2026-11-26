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
             billing_day=None, billing_cycle=Tenancy.CYCLE_MONTHLY,
             expected_check_out=None, agreement_note='',
             rent_mode=Tenancy.MODE_ADVANCE, prorate_first_period=True,
             first_invoice_amount=None):
    """Put a guest into a bed (or a whole room / flat) and occupy it."""
    # Default monthly billing to the check-in day; clamp 29–31 to the 28th
    # so every billing period has a matching day.
    if billing_day is None:
        billing_day = bill_day_for(check_in_date)
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


def _is_plain(invoice):
    """An invoice that is only rent and has no money on it: safe to raise again."""
    return (not invoice.payments.exists() and not invoice.payment_allocations.exists()
            and not invoice.lines.exists() and not invoice.discount_amount
            and not invoice.late_fee and not (invoice.status and invoice.status.counts_as_settled))


def plan_check_in_change(tenancy, old_date, new_date, on_date=None):
    """What moving the check-in date does to the stay's invoices, without doing it.

    Every plain rent invoice (nothing paid on it, no extras, discount, late fee
    or cancellation) is raised again from the new check-in date, so each month
    from check-in up to today has its invoice. An invoice with money on it or
    changed by hand stays exactly as it is, and only the days around it are
    billed. The bill day follows the new check-in day when it followed the old
    one and no invoice is kept (a kept invoice fixes the cycle).

    Returns ``{'drop', 'keep', 'raise', 'billing_day'}``: invoices to remove,
    invoices kept, and the ``billing.unbilled_rent`` rows to invoice.
    """
    import copy

    from billing.services import unbilled_rent

    invoices = list(tenancy.invoices.select_related('status').order_by('period_start'))
    keep = [invoice for invoice in invoices if not _is_plain(invoice)]
    drop = [invoice for invoice in invoices if _is_plain(invoice)]
    follows = (tenancy.billing_cycle == Tenancy.CYCLE_MONTHLY and not keep
               and tenancy.billing_day == bill_day_for(old_date))
    future = copy.copy(tenancy)
    future.check_in_date = new_date
    future.billing_day = bill_day_for(new_date) if follows else tenancy.billing_day
    raised = unbilled_rent(future, on_date, spans=[(i.period_start, i.period_end) for i in keep],
                           fill_gaps=True)
    same = ([(i.period_start, i.period_end, i.rent_amount) for i in drop]
            == [(r['period_start'], r['period_end'], r['rent']) for r in raised])
    if same and future.billing_day == tenancy.billing_day:
        # Already right: replacing the invoices would only change their numbers.
        drop, raised = [], []
    return {'drop': drop, 'keep': keep, 'raise': raised, 'billing_day': future.billing_day}


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
    return rebill_after_check_in_change(tenancy=tenancy, user=user, old_date=old_date,
                                        rebill=rebill)


@transaction.atomic
def rebill_after_check_in_change(*, tenancy, user, old_date, rebill=True):
    """After the check-in date moved: make the invoices match it.

    Applies ``plan_check_in_change`` — plain rent invoices raised again from
    the new date up to today, the rest kept. ``rebill`` false (no billing
    rights) leaves every invoice alone. Returns a note for the user.
    """
    from billing.services import apply_guest_credit, build_invoice, delete_invoice

    plan = plan_check_in_change(tenancy, old_date, tenancy.check_in_date)
    if not plan['drop'] and not plan['raise'] and plan['billing_day'] == tenancy.billing_day:
        return ''
    if not rebill:
        return _('The invoices were not changed: that needs billing rights.')
    for invoice in plan['drop']:
        delete_invoice(invoice=invoice, user=user)
    notes = []
    if plan['billing_day'] != tenancy.billing_day:
        before = audit.snapshot(tenancy, ['billing_day'])
        tenancy.billing_day = plan['billing_day']
        tenancy.save(update_fields=['billing_day', 'updated_at'])
        audit.log(audit.AuditLog.ACTION_UPDATE, tenancy, tenant=tenancy.tenant,
                  summary=_('Bill day of %(guest)s moved to day %(day)s with the check-in '
                            'date') % {'guest': tenancy.guest.full_name, 'day': tenancy.billing_day},
                  changes=audit.diff(before, audit.snapshot(tenancy, ['billing_day'])))
        notes.append(_('Rent is now billed on day %(day)s of each month.') % {
            'day': tenancy.billing_day})
    created = [build_invoice(tenancy, user=user, **row['terms']) for row in plan['raise']]
    if created:
        apply_guest_credit(guest=tenancy.guest, user=user)
        notes.insert(0, _('%(n)d invoice(s) from %(start)s to %(end)s: %(total)s.') % {
            'n': len(created), 'start': created[0].period_start.strftime('%d %b %Y'),
            'end': created[-1].period_end.strftime('%d %b %Y'),
            'total': sum((invoice.total for invoice in created), Decimal('0'))})
    if plan['keep']:
        notes.append(_('Kept as they were (money paid or changed by hand): %(numbers)s.') % {
            'numbers': ', '.join(invoice.number for invoice in plan['keep'])})
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
