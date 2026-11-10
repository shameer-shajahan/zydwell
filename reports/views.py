"""Dashboard, reports, exports, status settings and the audit log."""
from decimal import Decimal

from django.contrib import messages
from django.db.models import Case, Count, F, Q, Sum, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from accounting.models import KIND_EXPENSE, KIND_INCOME, LedgerEntry, totals
from accounting.views import filtered_entries
from billing.models import Invoice, Payment
from billing.services import MONEY, aging_buckets, owing, with_balance
from core import audit
from core.constants import MODULE_REPORTS
from core.filters import BED_PATHS, LocationFilter
from core.models import AuditLog, Status
from core.permissions import admin_required, require_module, signed_in_with_tenant
from core.utils import date_range_from_request, last_n_months, month_bounds, paginate
from guests.models import DepositTransaction, GuestDocument, Tenancy
from guests.services import with_deposit_held, with_outstanding
from properties.models import (
    Apartment, Bed, Building, Floor, Room, occupancy_for, occupancy_from_counts,
    occupancy_grouped, visible_beds, visible_buildings,
)

from .exports import excel_response, pdf_table_response
from .forms import StatusForm, TenantSettingsForm


@signed_in_with_tenant
def dashboard(request):
    """Everyone has a dashboard. An owner sees the whole business; a collector
    sees the floors they look after and what is theirs to do today."""
    from core.models import FeatureFlag

    flag = FeatureFlag.objects.filter(tenant=request.tenant, module=MODULE_REPORTS).first()
    reports_on = flag is None or flag.enabled
    if reports_on and request.user.can(MODULE_REPORTS, 'view'):
        return owner_dashboard(request)
    return staff_dashboard(request)


def staff_dashboard(request):
    """What one person has to get through today, inside their own patch."""
    from core.constants import (
        MODULE_BILLING, MODULE_GUESTS, MODULE_MESSAGING, MODULE_PROPERTIES,
    )
    from messaging.models import MessageLog

    user, tenant = request.user, request.tenant
    today = timezone.localdate()
    tiles, jobs = [], []

    if user.can(MODULE_PROPERTIES, 'view'):
        stats = occupancy_for(visible_beds(user, tenant))
        tiles.append({'label': _('Free beds in your area'), 'value': stats['available'],
                      'hint': _('of %(n)s beds') % {'n': stats['total']},
                      'tone': 'green', 'url': reverse('properties:bed_board')})

    live = None
    if user.can(MODULE_GUESTS, 'view'):
        live = user.scope_queryset(Tenancy.objects.filter(
            tenant=tenant, status__code__in=Tenancy.LIVE_STATUS_CODES))
        leaving = live.filter(expected_check_out__isnull=False,
                              expected_check_out__lte=today + timezone.timedelta(days=7))
        tiles.append({'label': _('Guests you look after'), 'value': live.count(),
                      'hint': _('%(n)s leaving within a week') % {'n': leaving.count()},
                      'tone': 'navy', 'url': reverse('guests:tenancy_list')})

    if user.can(MODULE_BILLING, 'view'):
        mine = Payment.objects.filter(tenant=tenant, collected_by=user, paid_on=today,
                                      is_reversed=False)
        collected = mine.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        tiles.append({'label': _('You collected today'),
                      'value': f'{tenant.currency} {collected:,.0f}',
                      'hint': _('%(n)s receipts') % {'n': mine.count()},
                      'tone': 'teal', 'url': reverse('billing:daily_collection')})

        owed = _owing(user.scope_queryset(
            Invoice.objects.filter(tenant=tenant),
            building_field='tenancy__building', floor_field='tenancy__floor',
            apartment_field='tenancy__apartment'))
        figures = owed.aggregate(total=Sum('due_balance'), n=Count('id'))
        tiles.append({'label': _('Rent still owed in your area'),
                      'value': f'{tenant.currency} {figures["total"] or Decimal("0"):,.0f}',
                      'hint': _('%(n)s invoices') % {'n': figures['n']},
                      'tone': 'red' if figures['n'] else 'slate',
                      'url': reverse('billing:overdue')})
        jobs = list(owed[:8])

    waiting = 0
    if user.can(MODULE_MESSAGING, 'view'):
        waiting = user.scope_queryset(
            MessageLog.objects.filter(tenant=tenant, status=MessageLog.STATUS_QUEUED),
            building_field='tenancy__building', floor_field='tenancy__floor',
            apartment_field='tenancy__apartment').count()
        tiles.append({'label': _('Reminders to send'), 'value': waiting,
                      'hint': _('open each one in WhatsApp'),
                      'tone': 'amber' if waiting else 'slate',
                      'url': reverse('messaging:waiting')})

    return render(request, 'reports/staff_dashboard.html', {
        'tiles': tiles,
        'jobs': jobs,
        'assignments': user.assignments.select_related('building', 'floor', 'apartment'),
        'recent': Payment.objects.filter(
            tenant=tenant, collected_by=user, is_reversed=False
        ).select_related('tenancy__guest')[:6] if user.can(MODULE_BILLING, 'view') else [],
        'can_check_in': user.can(MODULE_GUESTS, 'create'),
        'can_collect': user.can(MODULE_BILLING, 'create'),
        # Without this, the money cards would sit there empty and read as
        # "nothing is owed" when the truth is "you may not look".
        'can_see_money': user.can(MODULE_BILLING, 'view'),
        'month_label': today.strftime('%B %Y'),
    })


def owner_dashboard(request):
    tenant = request.tenant
    today = timezone.localdate()
    month_start, month_end = month_bounds(today)

    beds = visible_beds(request.user, tenant)
    occupancy = occupancy_for(beds)

    payments = Payment.objects.filter(
        tenant=tenant, is_reversed=False, paid_on__range=(month_start, month_end)
    )
    collected = payments.aggregate(s=Sum('amount'))['s'] or Decimal('0')

    open_invoices = Invoice.objects.filter(tenant=tenant).exclude(status__counts_as_settled=True)
    pending = with_balance(open_invoices).aggregate(
        s=Sum('due_balance'))['s'] or Decimal('0')
    buckets = aging_buckets(tenant)

    ledger_month = LedgerEntry.objects.filter(
        tenant=tenant, entry_date__range=(month_start, month_end)
    )
    money = totals(ledger_month)
    expense_split = (
        ledger_month.filter(kind=KIND_EXPENSE)
        .values('category__name').annotate(total=Sum('amount')).order_by('-total')[:8]
    )

    months = last_n_months(12, today)
    trend = []
    peak = Decimal('1')
    for _year, _month, label, start, end in months:
        rows = LedgerEntry.objects.filter(tenant=tenant, entry_date__range=(start, end))
        figures = totals(rows)
        peak = max(peak, figures['income'], figures['expense'])
        trend.append({'label': label, 'income': figures['income'], 'expense': figures['expense']})
    for point in trend:
        point['income_pct'] = int(point['income'] / peak * 100) if peak else 0
        point['expense_pct'] = int(point['expense'] / peak * 100) if peak else 0

    floor_rows = Floor.objects.filter(
        building__in=visible_buildings(request.user, tenant), is_archived=False,
    ).select_related('building').order_by('building__name', 'building_id', 'sort_order', 'name')
    by_floor = occupancy_grouped(Bed.objects.filter(room__apartment__floor__in=floor_rows),
                                 'room__apartment__floor')
    floors = [{'floor': floor, 'stats': by_floor[floor.pk]}
              for floor in floor_rows if by_floor.get(floor.pk, {}).get('total')]

    from guests.services import deposit_totals

    awaiting = Payment.objects.filter(tenant=tenant, approval=Payment.APPROVAL_PENDING)
    awaiting_deposits = DepositTransaction.objects.filter(tenant=tenant,
                                                          approval='pending')

    deposits_all = deposit_totals(tenant)
    deposits_month = deposit_totals(tenant, month_start, month_end)

    expiring = GuestDocument.objects.filter(
        tenant=tenant, expiry_date__isnull=False,
        expiry_date__lte=today + timezone.timedelta(days=30),
    ).select_related('guest')[:8]

    return render(request, 'reports/dashboard.html', {
        'occupancy': occupancy,
        'collected': collected,
        'pending': pending,
        'buckets': buckets,
        'aging_rows': [(label, buckets[label]) for label in
                       ('current', '0-30', '31-60', '60+')],
        'oldest_due': buckets['60+'],
        'money': money,
        'deposits_held': deposits_all['net'],
        'awaiting_count': awaiting.count() + awaiting_deposits.count(),
        'awaiting_total': (awaiting.aggregate(s=Sum('amount'))['s'] or Decimal('0'))
                          + (awaiting_deposits.aggregate(s=Sum('amount'))['s']
                             or Decimal('0')),
        'deposit_month': deposits_month,
        'expense_split': expense_split,
        'trend': trend,
        'floors': floors,
        'expiring': expiring,
        'month_label': today.strftime('%B %Y'),
        'live_tenancies': Tenancy.objects.filter(
            tenant=tenant, status__code__in=Tenancy.LIVE_STATUS_CODES).count(),
        'recent_payments': Payment.objects.filter(
            tenant=tenant, is_reversed=False
        ).select_related('tenancy__guest', 'collected_by')[:8],
        'overdue_top': list(_owing(Invoice.objects.filter(tenant=tenant))[:8]),
    })


def _owing(invoices):
    """Unpaid invoices in overdue order, with what each row prints already loaded."""
    return owing(invoices).select_related(
        'tenancy__guest', 'tenancy__building', 'tenancy__floor', 'tenancy__apartment',
        'tenancy__room', 'tenancy__bed')


@require_module(MODULE_REPORTS)
def report_index(request):
    return render(request, 'reports/index.html', {})


def _rent_roll(request):
    """Live tenancies the request asks for: the user's area, the picked place, the search."""
    location = LocationFilter(request)
    rows = request.user.scope_queryset(Tenancy.objects.filter(
        tenant=request.tenant, status__code__in=Tenancy.LIVE_STATUS_CODES,
    ).select_related('guest', 'room', 'apartment', 'floor', 'building', 'bed', 'status'))
    rows = location.apply(rows)
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(Q(guest__full_name__icontains=query) | Q(guest__mobile__icontains=query)
                           | Q(room__number__icontains=query))
    return rows, location, query


@require_module(MODULE_REPORTS)
def rent_roll(request):
    rows, location, query = _rent_roll(request)
    figures = rows.aggregate(rent=Sum('monthly_rent'), live=Count('id'))
    due = Invoice.objects.filter(tenancy__in=rows.values('pk')).exclude(
        status__counts_as_settled=True,
    ).aggregate(s=Sum(F('total') - F('paid_amount'), output_field=MONEY))['s']
    return render(request, 'reports/rent_roll.html', {
        'data': paginate(request, with_deposit_held(with_outstanding(rows)), 50),
        'live_count': figures['live'],
        'location': location, 'query': query,
        'total_rent': figures['rent'] or Decimal('0'),
        'total_due': due or Decimal('0'),
    })


def _payments_in_range(request):
    location = LocationFilter(request)
    start, end = date_range_from_request(request)
    rows = Payment.objects.filter(
        tenant=request.tenant, paid_on__range=(start, end),
    ).select_related('tenancy__guest', 'tenancy__building', 'invoice', 'collected_by', 'mode')
    rows = request.user.scope_queryset(
        rows, building_field='tenancy__building', floor_field='tenancy__floor',
        apartment_field='tenancy__apartment')
    return location.apply(rows, prefix='tenancy__'), location, start, end


@require_module(MODULE_REPORTS)
def collection_report(request):
    rows, location, start, end = _payments_in_range(request)
    rows = rows.filter(is_reversed=False)
    by_collector = rows.values('collected_by_name').annotate(
        total=Sum('amount'), n=Count('id')).order_by('-total')
    by_mode = rows.values('mode__name').annotate(
        total=Sum('amount'), n=Count('id')).order_by('-total')
    by_building = rows.values('tenancy__building__name').annotate(
        total=Sum('amount')).order_by('-total')
    return render(request, 'reports/collection.html', {
        'rows': paginate(request, rows, 50), 'start': start, 'end': end,
        'by_collector': by_collector, 'by_mode': by_mode, 'by_building': by_building,
        'total': rows.aggregate(s=Sum('amount'))['s'] or Decimal('0'),
        'location': location,
    })


def _ledger_in_range(request, depth='room'):
    location = LocationFilter(request, depth=depth)
    start, end = date_range_from_request(request)
    rows = request.user.scope_queryset(LedgerEntry.objects.filter(
        tenant=request.tenant, entry_date__range=(start, end)))
    return location.apply(rows), location, start, end


@require_module(MODULE_REPORTS)
def expense_report(request):
    rows, location, start, end = _ledger_in_range(request)
    rows = rows.filter(kind=KIND_EXPENSE).select_related(
        'category', 'building', 'apartment__floor__building', 'room__apartment__floor__building')
    by_category = rows.values('category__name').annotate(
        total=Sum('amount'), n=Count('id')).order_by('-total')
    by_building = rows.values('building__name').annotate(total=Sum('amount')).order_by('-total')
    grand = rows.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    for row in by_category:
        row['pct'] = int((row['total'] / grand) * 100) if grand else 0
    return render(request, 'reports/expense.html', {
        'rows': paginate(request, rows, 50), 'start': start, 'end': end,
        'by_category': by_category, 'by_building': by_building, 'total': grand,
        'location': location,
    })


@require_module(MODULE_REPORTS)
def profit_report(request):
    base, location, start, end = _ledger_in_range(request)
    summary = totals(base)

    # Summed per place by the database; only the names are looked up after.
    groups = list(base.order_by().values('room', 'apartment', 'floor', 'building', 'kind')
                  .annotate(total=Sum('amount')))
    places = {
        'room': Room.objects.select_related('apartment__floor__building').in_bulk(
            {g['room'] for g in groups if g['room']}),
        'apartment': Apartment.objects.select_related('floor__building').in_bulk(
            {g['apartment'] for g in groups if g['apartment']}),
        'floor': Floor.objects.select_related('building').in_bulk(
            {g['floor'] for g in groups if g['floor']}),
        'building': Building.objects.in_bulk({g['building'] for g in groups if g['building']}),
    }
    per_place = {}
    for group in groups:
        level = next((name for name in ('room', 'apartment', 'floor', 'building')
                      if group[name]), None)
        label = str(places[level][group[level]]) if level else _('Untagged')
        bucket = per_place.setdefault(label, {'income': Decimal('0'), 'expense': Decimal('0')})
        bucket['income' if group['kind'] == KIND_INCOME else 'expense'] += group['total']
    rows = [
        {'label': label, 'income': v['income'], 'expense': v['expense'],
         'net': v['income'] - v['expense']}
        for label, v in per_place.items()
    ]
    rows.sort(key=lambda r: -r['net'])
    return render(request, 'reports/profit.html', {
        'summary': summary, 'rows': paginate(request, rows, 50), 'start': start, 'end': end,
        'location': location,
    })


def _deposit_rows(request, location, start=None, end=None):
    rows = DepositTransaction.objects.filter(tenant=request.tenant)
    if start and end:
        rows = rows.filter(happened_on__range=(start, end))
    rows = request.user.scope_queryset(
        rows, building_field='tenancy__building', floor_field='tenancy__floor',
        apartment_field='tenancy__apartment')
    return location.apply(rows, prefix='tenancy__')


@require_module(MODULE_REPORTS)
def deposit_report(request):
    """Deposit money in and out — held, kept and given back."""
    from guests.services import deposit_totals

    start, end = date_range_from_request(request)
    location = LocationFilter(request)
    in_range = _deposit_rows(request, location, start, end)
    every = _deposit_rows(request, location)
    period = deposit_totals(request.tenant, rows=in_range)
    overall = deposit_totals(request.tenant, rows=every)
    rows = in_range.select_related(
        'tenancy__guest', 'tenancy__building', 'tenancy__room', 'recorded_by'
    ).order_by('-happened_on', '-id')

    signed = Case(
        When(kind=DepositTransaction.KIND_HOLD, then=F('amount')),
        default=-F('amount'), output_field=MONEY)
    by_building = [
        (row['tenancy__building__name'], row['held'])
        for row in every.order_by().values('tenancy__building__name')
        .annotate(held=Sum(signed)).order_by('-held')
    ]

    return render(request, 'reports/deposits.html', {
        'period': period, 'overall': overall, 'rows': paginate(request, rows, 50),
        'start': start, 'end': end, 'location': location,
        'by_building': by_building,
    })


@require_module(MODULE_REPORTS)
def export_deposits(request):
    from guests.services import deposit_totals

    start, end = date_range_from_request(request)
    rows = _deposit_rows(request, LocationFilter(request), start, end).select_related(
        'tenancy__guest', 'tenancy__building', 'tenancy__room', 'recorded_by')
    headers = [_('Receipt'), _('Date'), _('Guest'), _('Building'), _('Room'), _('Movement'),
               _('In'), _('Out'), _('Reason'), _('Recorded by')]
    data = [[
        entry.receipt_number, entry.happened_on, entry.tenancy.guest.full_name,
        entry.tenancy.building.name, entry.tenancy.room.number,
        entry.get_kind_display(),
        float(entry.amount) if entry.is_money_in else '',
        '' if entry.is_money_in else float(entry.amount),
        entry.reason,
        entry.recorded_by.display_name() if entry.recorded_by_id else '',
    ] for entry in rows]
    audit.log(AuditLog.ACTION_EXPORT, None, tenant=request.tenant,
              model_name='DepositTransaction', object_label='deposits',
              summary=_('Exported the deposit report'))
    if request.GET.get('format') == 'pdf':
        return pdf_table_response('deposits', str(_('Deposits')),
                                  f'{start:%d %b %Y} – {end:%d %b %Y}', headers, data)
    return excel_response('deposits', [(_('Deposits'), headers, data)])


@require_module(MODULE_REPORTS)
def occupancy_report(request):
    location = LocationFilter(request, depth='apartment')
    chain = location.chain
    buildings = visible_buildings(request.user, request.tenant)
    floors = Floor.objects.filter(building__in=buildings, is_archived=False)
    apartments = Apartment.objects.filter(floor__in=floors, is_archived=False)
    if 'building' in chain:
        buildings = buildings.filter(pk=chain['building'].pk)
    if 'floor' in chain:
        floors = floors.filter(pk=chain['floor'].pk)
    if 'apartment' in chain:
        apartments = apartments.filter(pk=chain['apartment'].pk)

    # Counted like Building/Floor/Apartment.occupancy(), one grouped query a level.
    beds = location.apply(Bed.objects.filter(
        tenant=request.tenant, room__apartment__floor__building__in=buildings,
    ), paths=BED_PATHS)
    empty = occupancy_from_counts(0, 0, 0)
    by_building = occupancy_grouped(beds, BED_PATHS['building'])
    by_floor = occupancy_grouped(beds, BED_PATHS['floor'])
    by_flat = occupancy_grouped(beds, BED_PATHS['apartment'])

    flats = {}
    for flat in apartments:
        flats.setdefault(flat.floor_id, []).append(
            {'apartment': flat, 'stats': by_flat.get(flat.pk, empty)})
    per_building = {}
    for floor in floors.filter(building__in=buildings):
        per_building.setdefault(floor.building_id, []).append({
            'floor': floor, 'stats': by_floor.get(floor.pk, empty),
            'apartments': flats.get(floor.pk, []),
        })
    blocks = [{'building': building, 'stats': by_building.get(building.pk, empty),
               'floors': per_building.get(building.pk, [])} for building in buildings]
    return render(request, 'reports/occupancy.html', {
        'blocks': blocks,
        'location': location,
        'overall': occupancy_for(location.apply(
            visible_beds(request.user, request.tenant), paths=BED_PATHS)),
    })


# --- Exports ----------------------------------------------------------
@require_module(MODULE_REPORTS)
def export_rent_roll(request):
    rows = with_deposit_held(with_outstanding(_rent_roll(request)[0]))
    headers = [_('Guest'), _('Mobile'), _('Building'), _('Floor'), _('Apartment'),
               _('Room'), _('Bed'), _('Check-in'), _('Monthly rent'), _('Deposit held'),
               _('Outstanding')]
    data = [[
        t.guest.full_name, t.guest.mobile, t.building.name, t.floor.name,
        t.apartment.name if t.apartment_id else '', t.room.number,
        t.bed.label if t.bed_id else _('Whole unit'), t.check_in_date,
        float(t.monthly_rent), float(t.deposit_held_total), float(t.outstanding_total),
    ] for t in rows]
    audit.log(AuditLog.ACTION_EXPORT, None, tenant=request.tenant, model_name='Tenancy',
              object_label='rent-roll', summary=_('Exported the rent roll'))
    if request.GET.get('format') == 'pdf':
        return pdf_table_response('rent-roll', str(_('Rent roll')),
                                  f'{request.tenant.name} · {timezone.localdate():%d %b %Y}',
                                  headers, data)
    return excel_response('rent-roll', [(_('Rent roll'), headers, data)])


@require_module(MODULE_REPORTS)
def export_collection(request):
    rows, _location, start, end = _payments_in_range(request)
    headers = [_('Receipt'), _('Date'), _('Guest'), _('Invoice'), _('Mode'), _('Reference'),
               _('Collected by'), _('Amount'), _('Reversed')]
    data = [[
        p.receipt_number, p.paid_on, p.tenancy.guest.full_name,
        p.invoice.number if p.invoice_id else _('Advance'), str(p.mode or ''), p.reference,
        p.collected_by_name, float(p.amount), _('Yes') if p.is_reversed else '',
    ] for p in rows]
    audit.log(AuditLog.ACTION_EXPORT, None, tenant=request.tenant, model_name='Payment',
              object_label='collection', summary=_('Exported the collection report'))
    if request.GET.get('format') == 'pdf':
        return pdf_table_response('collection', str(_('Collection report')),
                                  f'{start:%d %b %Y} – {end:%d %b %Y}', headers, data)
    return excel_response('collection', [(_('Collection'), headers, data)])


@require_module(MODULE_REPORTS)
def export_ledger(request):
    rows, filters = filtered_entries(request)
    start, end = filters['start'], filters['end']
    headers = [_('Date'), _('Ledger'), _('Category'), _('Description'), _('Building'),
               _('Floor'), _('Room'), _('Payee'), _('Source'), _('Amount')]
    data = [[
        e.entry_date, e.get_kind_display(), e.category.name, e.description,
        e.building.name if e.building_id else '', e.floor.name if e.floor_id else '',
        e.room.number if e.room_id else '', e.payee, e.get_source_type_display(),
        float(e.amount),
    ] for e in rows]
    audit.log(AuditLog.ACTION_EXPORT, None, tenant=request.tenant, model_name='LedgerEntry',
              object_label='ledger', summary=_('Exported the income & expense ledger'))
    if request.GET.get('format') == 'pdf':
        return pdf_table_response('ledger', str(_('Income & expense')),
                                  f'{start:%d %b %Y} – {end:%d %b %Y}', headers, data)
    return excel_response('ledger', [(_('Ledger'), headers, data)])


@admin_required
def export_everything(request):
    """One workbook with every module — the 'Excel export of all modules' requirement."""
    tenant = request.tenant
    from accounts.models import User
    from guests.models import Guest

    sheets = [
        (_('Buildings'), [_('Name'), _('City'), _('Owner'), _('Licence'), _('Phone')],
         [[b.name, b.city, b.owner_name, b.licence_number, b.contact_phone]
          for b in Building.objects.filter(tenant=tenant)]),
        (_('Apartments'), [_('Building'), _('Floor'), _('Apartment'), _('Rooms')],
         [[a.floor.building.name, a.floor.name, a.name, a.room_count]
          for a in Apartment.objects.filter(tenant=tenant)
              .select_related('floor__building').annotate(room_count=Count('rooms'))]),
        (_('Rooms'), [_('Building'), _('Floor'), _('Apartment'), _('Room'), _('Type'),
                      _('Capacity'), _('Rent basis'), _('Monthly rent')],
         [[r.apartment.floor.building.name, r.apartment.floor.name, r.apartment.name,
           r.number, r.room_type.name if r.room_type_id else '', r.capacity,
           r.get_rent_basis_display(), float(r.monthly_rent)]
          for r in Room.objects.filter(tenant=tenant).select_related(
              'apartment__floor__building', 'room_type')]),
        (_('Beds'), [_('Building'), _('Apartment'), _('Room'), _('Bed'), _('Price'),
                     _('Status')],
         [[b.room.apartment.floor.building.name, b.room.apartment.name, b.room.number,
           b.label, float(b.monthly_price), b.status.name if b.status_id else '']
          for b in Bed.objects.filter(tenant=tenant).select_related(
              'room__apartment__floor__building', 'status')]),
        (_('Guests'), [_('Name'), _('Mobile'), _('Email'), _('Nationality'), _('Employer')],
         [[g.full_name, g.mobile, g.email, g.nationality, g.employer]
          for g in Guest.objects.filter(tenant=tenant)]),
        (_('Tenancies'), [_('Guest'), _('Location'), _('Check-in'), _('Check-out'), _('Rent'),
                          _('Status')],
         [[t.guest.full_name, t.location_label(), t.check_in_date, t.actual_check_out or '',
           float(t.monthly_rent), t.status.name if t.status_id else '']
          for t in Tenancy.objects.filter(tenant=tenant).select_related(
              'guest', 'room', 'apartment', 'floor', 'building', 'bed', 'status')]),
        (_('Invoices'), [_('Number'), _('Guest'), _('Period'), _('Due'), _('Total'), _('Paid'),
                         _('Balance'), _('Status')],
         [[i.number, i.tenancy.guest.full_name, i.period_label(), i.due_date, float(i.total),
           float(i.paid_amount), float(i.balance), i.status.name if i.status_id else '']
          for i in Invoice.objects.filter(tenant=tenant).select_related('tenancy__guest',
                                                                        'status')]),
        (_('Payments'), [_('Receipt'), _('Date'), _('Guest'), _('Mode'), _('Amount'),
                         _('Collected by'), _('Reversed')],
         [[p.receipt_number, p.paid_on, p.tenancy.guest.full_name, str(p.mode or ''),
           float(p.amount), p.collected_by_name, _('Yes') if p.is_reversed else '']
          for p in Payment.objects.filter(tenant=tenant).select_related(
              'tenancy__guest', 'mode')]),
        (_('Deposits'), [_('Receipt'), _('Date'), _('Guest'), _('Movement'), _('In'),
                         _('Out'), _('Reason')],
         [[d.receipt_number, d.happened_on, d.tenancy.guest.full_name,
           d.get_kind_display(), float(d.amount) if d.is_money_in else '',
           '' if d.is_money_in else float(d.amount), d.reason]
          for d in DepositTransaction.objects.filter(tenant=tenant)
              .select_related('tenancy__guest')]),
        (_('Ledger'), [_('Date'), _('Kind'), _('Category'), _('Amount'), _('Tagged to')],
         [[e.entry_date, e.get_kind_display(), e.category.name, float(e.amount),
           str(e.room or e.floor or e.building or '')]
          for e in LedgerEntry.objects.filter(tenant=tenant).select_related(
              'category', 'building', 'floor__building',
              'room__apartment__floor__building')]),
        (_('Staff'), [_('Name'), _('Username'), _('Phone'), _('Active')],
         [[u.display_name(), u.username, u.phone, _('Yes') if u.is_active else _('No')]
          for u in User.objects.filter(tenant=tenant)]),
    ]
    audit.log(AuditLog.ACTION_EXPORT, None, tenant=tenant, model_name='Tenant',
              object_label=tenant.name, summary=_('Exported every module to Excel'))
    return excel_response('zydwell-full-export', sheets)


# --- Settings ---------------------------------------------------------
@admin_required
def settings_home(request):
    audited = ['currency', 'date_format', 'tax_percent', 'name']
    before = audit.snapshot(request.tenant, audited)
    form = TenantSettingsForm(request.POST or None, instance=request.tenant)
    if request.method == 'POST' and form.is_valid():
        form.save()
        audit.log(AuditLog.ACTION_UPDATE, request.tenant, summary=_('Account settings updated'),
                  changes=audit.diff(before, audit.snapshot(request.tenant, audited)))
        messages.success(request, _('Settings saved.'))
        return redirect('reports:settings_home')
    return render(request, 'reports/settings.html', {
        'form': form,
        'limits': request.tenant.limit_report(),
        'subscription': getattr(request.tenant, 'subscription', None),
    })


@admin_required
def status_list(request):
    kind = request.GET.get('kind', Status.KIND_BED)
    form = StatusForm(request.POST or None, initial={'kind': kind})
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        if Status.objects.filter(tenant=request.tenant, kind=obj.kind, code=obj.code).exists():
            messages.error(request, _('That code already exists for this kind.'))
        else:
            obj.save()
            audit.log(AuditLog.ACTION_CREATE, obj,
                      summary=_('Status %(n)s added') % {'n': obj.name})
            messages.success(request, _('Status added.'))
            return redirect(f"{reverse('reports:status_list')}?kind={obj.kind}")
    return render(request, 'reports/statuses.html', {
        'kind': kind,
        'kinds': Status.KIND_CHOICES,
        'rows': paginate(request, Status.objects.filter(tenant=request.tenant, kind=kind), 25),
        'form': form,
    })


@admin_required
def status_edit(request, pk):
    status = get_object_or_404(Status, pk=pk, tenant=request.tenant)
    before = audit.snapshot(status)
    form = StatusForm(request.POST or None, instance=status)
    if request.method == 'POST' and form.is_valid():
        obj = form.save()
        audit.log(AuditLog.ACTION_UPDATE, obj, summary=_('Status %(n)s updated') % {'n': obj.name},
                  changes=audit.diff(before, audit.snapshot(obj)))
        messages.success(request, _('Status saved.'))
        return redirect(f"{reverse('reports:status_list')}?kind={obj.kind}")
    return render(request, 'reports/status_form.html', {'form': form, 'status': status})


@admin_required
def status_delete(request, pk):
    status = get_object_or_404(Status, pk=pk, tenant=request.tenant)
    kind = status.kind
    if request.method == 'POST':
        if status.is_protected:
            messages.error(request, _('This status is used by automations and cannot be removed.'))
        elif status.beds.exists() or status.tenancies.exists() or status.invoices.exists():
            messages.error(request, _('Records still use this status. Move them first.'))
        else:
            audit.log(AuditLog.ACTION_DELETE, status,
                      summary=_('Status %(n)s deleted') % {'n': status.name})
            status.delete()
            messages.success(request, _('Status deleted.'))
    return redirect(f"{reverse('reports:status_list')}?kind={kind}")


@admin_required
def audit_log(request):
    rows = AuditLog.objects.filter(tenant=request.tenant).select_related('actor')
    action = request.GET.get('action', '')
    if action:
        rows = rows.filter(action=action)
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(
            Q(summary__icontains=query) | Q(object_label__icontains=query)
            | Q(actor_label__icontains=query) | Q(model_name__icontains=query)
        )
    return render(request, 'reports/audit_log.html', {
        'page': paginate(request, rows, 40),
        'actions': AuditLog.ACTION_CHOICES,
        'action': action, 'query': query,
    })
