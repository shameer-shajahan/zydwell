"""Check-in, check-out, bed transfer and deposit settlement."""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import (
    Case, DecimalField, F, OuterRef, Subquery, Sum, Value, When,
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


def _occupy(tenancy):
    if tenancy.bed_id:
        tenancy.bed.set_status_code('occupied')
    else:
        for bed in tenancy.room.beds.filter(is_archived=False):
            bed.set_status_code('occupied')


def _release(tenancy):
    if tenancy.bed_id:
        tenancy.bed.set_status_code('available')
    else:
        for bed in tenancy.room.beds.filter(is_archived=False):
            bed.set_status_code('available')


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
