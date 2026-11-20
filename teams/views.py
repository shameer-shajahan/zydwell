"""Team lets: list with totals, create / edit, detail, CSV import, raising invoices."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core.constants import MODULE_BILLING, MODULE_GUESTS
from core.filters import LocationFilter
from core.permissions import require_module
from core.utils import month_bounds, redirect_back

from . import importer, services
from .forms import TeamImportForm, TeamLetForm
from .models import TeamLet

MAX_IMPORT_BYTES = 512 * 1024


def _scoped_teams(request):
    qs = TeamLet.objects.filter(tenant=request.tenant).select_related(
        'guest', 'apartment', 'room', 'floor', 'building', 'status')
    return request.user.scope_queryset(qs)


@require_module(MODULE_GUESTS)
def team_list(request):
    location = LocationFilter(request, depth='apartment')
    rows = location.apply(_scoped_teams(request))
    state = request.GET.get('state', 'live')
    if state == 'live':
        rows = rows.live()
    elif state == 'vacated':
        rows = rows.filter(status__code='vacated')
    query = request.GET.get('q', '').strip()
    if query:
        rows = services.search(rows, query)
    month_start, month_end = month_bounds(timezone.localdate())
    rows = list(services.with_money(rows, month_start, month_end))
    return render(request, 'teams/team_list.html', {
        'rows': rows,
        'totals': services.totals(rows),
        'state': state, 'query': query, 'location': location,
        'month_label': month_start.strftime('%B %Y'),
        'can_create': request.user.can(MODULE_GUESTS, 'create'),
        'can_bill': request.user.can(MODULE_BILLING, 'create'),
    })


def _save_form(request, form, team=None):
    data = form.cleaned_data
    company = data['company']
    if company is None:
        company, _new = services.get_or_create_company(
            request.tenant, name=data['company_name'],
            mobile=data.get('collector_mobile') or '',
            contact_person=data.get('collector_name') or '')
    fields = {
        'team_name': data['team_name'], 'joining_date': data['joining_date'],
        'billing_day': data['billing_day'], 'members': data['members'],
        'per_head': data['per_head'], 'monthly_rent': data['monthly_rent'],
        'deposit_amount': data.get('deposit_amount') or 0,
        'agreement_note': data['agreement_note'],
        'prorate_first_period': data['prorate_first_period'],
    }
    room = data['room']
    if team is None:
        return services.create_team_let(
            tenant=request.tenant, user=request.user, company=company,
            apartment=data['apartment'], room=room,
            deposit_received=data.get('deposit_received') or 0,
            **fields)
    return services.update_team_let(team=team, user=request.user, company=company,
                                    apartment=data['apartment'], room=room, **fields)


def _flash_headcount(request, form):
    expected = form.headcount_note()
    if expected is not None:
        messages.warning(request, _(
            'Members × per head comes to %(calc)s, not the %(rent)s agreed. The agreed rent '
            'is what is billed.') % {'calc': expected,
                                     'rent': form.cleaned_data['monthly_rent']})


@require_module(MODULE_GUESTS, 'create')
def team_create(request):
    initial = {}
    if request.GET.get('apartment', '').isdigit():
        initial['apartment'] = request.GET['apartment']
    form = TeamLetForm(request.POST or None, tenant=request.tenant, user=request.user,
                       initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            team = _save_form(request, form)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, _('Place %(place)s let to team %(team)s.') % {
                'place': team.room or team.apartment, 'team': team.team_name})
            _flash_headcount(request, form)
            return redirect_back(request, 'teams:team_detail', pk=team.pk, made=team)
    return render(request, 'teams/team_form.html', {'form': form, 'team': None})


@require_module(MODULE_GUESTS, 'edit')
def team_edit(request, pk):
    team = get_object_or_404(_scoped_teams(request), pk=pk)
    form = TeamLetForm(request.POST or None, tenant=request.tenant, user=request.user,
                       team=team, initial=TeamLetForm.initial_for(team))
    if request.method == 'POST' and form.is_valid():
        try:
            _save_form(request, form, team=team)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, _('Team let updated.'))
            _flash_headcount(request, form)
            return redirect('teams:team_detail', pk=team.pk)
    return render(request, 'teams/team_form.html', {'form': form, 'team': team})


@require_module(MODULE_GUESTS)
def team_detail(request, pk):
    from billing.models import Invoice, Payment
    from core.utils import paginate
    from guests.forms import DepositForm

    team = get_object_or_404(_scoped_teams(request), pk=pk)
    return render(request, 'teams/team_detail.html', {
        'team': team,
        'invoices': paginate(request, Invoice.objects.filter(
            tenancy=team).select_related('status'), 12, param='invoice_page'),
        'payments': paginate(request, Payment.objects.filter(
            tenancy=team).select_related('mode'), 12, param='payment_page'),
        'deposits': team.deposit_transactions.all()[:20],
        'deposit_held': team.deposit_held(),
        'outstanding': team.outstanding(),
        'others': TeamLet.objects.live().filter(apartment=team.apartment_id).exclude(
            pk=team.pk).select_related('guest'),
        'beds': (team.room.beds.filter(is_archived=False).count() if team.room_id
                 else team.apartment.bed_queryset.count()),
        'deposit_form': DepositForm(tenant=request.tenant),
        'can_edit': request.user.can(MODULE_GUESTS, 'edit'),
        'can_bill': request.user.can(MODULE_BILLING, 'create'),
    })


@require_module(MODULE_BILLING, 'create')
@require_POST
def raise_invoices(request):
    teams = _scoped_teams(request)
    created = services.raise_invoices(tenant=request.tenant, user=request.user, teams=teams)
    if created:
        messages.success(request, _('%(n)s team invoices raised for this period.') % {
            'n': len(created)})
    else:
        messages.info(request, _('Every live team already has an invoice for this period.'))
    return redirect_back(request, 'teams:team_list')


@require_module(MODULE_GUESTS, 'create')
def team_import(request):
    """Upload → preview every row → confirm. The confirm step re-reads the same text."""
    form = TeamImportForm(request.POST or None, request.FILES or None)
    plan, text, result = None, '', None
    if request.method == 'POST':
        if request.POST.get('csv_text'):
            text = request.POST['csv_text']
        elif form.is_valid():
            upload = form.cleaned_data['file']
            if upload.size > MAX_IMPORT_BYTES:
                form.add_error('file', _('The file is larger than 512 KB.'))
            else:
                raw = upload.read()
                try:
                    text = raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    text = raw.decode('cp1252', errors='replace')
        if text:
            plan = importer.read(text, request.tenant)
            if request.POST.get('apply') and plan.ok:
                result = importer.apply(plan, request.tenant, request.user)
                messages.success(request, _(
                    'Import saved: %(created)s new team lets, %(updated)s updated.') % result)
                return redirect('teams:team_list')
            if request.POST.get('apply'):
                messages.error(request, _('Nothing was saved — fix the rows marked as errors '
                                          'and upload again.'))
    return render(request, 'teams/team_import.html', {
        'form': form, 'plan': plan, 'csv_text': text,
        'columns': importer.REQUIRED_COLUMNS, 'optional': importer.OPTIONAL_COLUMNS,
    })
