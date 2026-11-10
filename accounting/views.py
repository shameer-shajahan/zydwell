"""The income and expense ledger, categories and recurring bills."""
from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import audit
from core.constants import MODULE_ACCOUNTING
from core.filters import LocationFilter
from core.models import AuditLog
from core.permissions import admin_required, require_module
from core.utils import date_range_from_request, paginate, redirect_back

from .forms import CategoryForm, LedgerEntryForm, RecurringExpenseForm
from .models import KIND_EXPENSE, KIND_INCOME, Category, LedgerEntry, RecurringExpense, totals


def _scoped_entries(request):
    # The "tagged to" column prints the full path of the narrowest place.
    qs = LedgerEntry.objects.filter(tenant=request.tenant).select_related(
        'category', 'building', 'floor__building', 'apartment__floor__building',
        'room__apartment__floor__building', 'recorded_by'
    )
    return request.user.scope_queryset(qs)


def filtered_entries(request):
    """The ledger rows a request asks for — shared by the screen and its export.

    Returns ``(rows, filters)``; ``filters`` holds what the toolbar shows.
    """
    location = LocationFilter(request, depth='room')
    start, end = date_range_from_request(request)
    rows = location.apply(_scoped_entries(request)).filter(entry_date__range=(start, end))
    kind = request.GET.get('kind', '')
    if kind in (KIND_INCOME, KIND_EXPENSE):
        rows = rows.filter(kind=kind)
    category_id = request.GET.get('category', '')
    if category_id.isdigit():
        rows = rows.filter(category_id=category_id)
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(Q(description__icontains=query) | Q(payee__icontains=query)
                           | Q(reference__icontains=query) | Q(category__name__icontains=query))
    return rows, {'location': location, 'start': start, 'end': end, 'kind': kind,
                  'category_id': category_id, 'query': query}


@require_module(MODULE_ACCOUNTING)
def ledger(request):
    rows, filters = filtered_entries(request)
    by_category = (
        rows.values('category__name', 'kind')
        .annotate(total=Sum('amount')).order_by('-total')
    )
    return render(request, 'accounting/ledger.html', {
        **filters,
        'page': paginate(request, rows, 30),
        'summary': totals(rows),
        'by_category': by_category,
        'categories': Category.objects.filter(tenant=request.tenant, is_active=True),
        'can_create': request.user.can(MODULE_ACCOUNTING, 'create'),
        'can_delete': request.user.can(MODULE_ACCOUNTING, 'delete'),
        'deletable_sources': LedgerEntry.DELETABLE_SOURCES,
    })


@require_module(MODULE_ACCOUNTING, 'create')
def entry_form(request, pk=None):
    entry = get_object_or_404(LedgerEntry, pk=pk, tenant=request.tenant) if pk else None
    if entry and entry.source_type != LedgerEntry.SOURCE_MANUAL:
        messages.error(request, _('Automatic entries cannot be edited here.'))
        return redirect('accounting:ledger')
    before = audit.snapshot(entry) if entry else {}
    initial = {'kind': request.GET.get('kind', KIND_EXPENSE)}
    form = LedgerEntryForm(request.POST or None, request.FILES or None, instance=entry,
                           tenant=request.tenant, user=request.user, initial=initial)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        obj.recorded_by = obj.recorded_by or request.user
        obj.save()
        audit.log(AuditLog.ACTION_UPDATE if entry else AuditLog.ACTION_CREATE, obj,
                  summary=_('%(kind)s %(amount)s — %(cat)s') % {
                      'kind': obj.get_kind_display(), 'amount': obj.amount,
                      'cat': obj.category.name},
                  changes=audit.diff(before, audit.snapshot(obj)) if entry else {})
        messages.success(request, _('Entry saved.'))
        return redirect_back(request, 'accounting:ledger', made=obj)
    return render(request, 'accounting/entry_form.html', {'form': form, 'entry': entry})


@require_module(MODULE_ACCOUNTING, 'delete')
def entry_delete(request, pk):
    entry = get_object_or_404(LedgerEntry, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        if entry.source_type not in LedgerEntry.DELETABLE_SOURCES:
            messages.error(request, _('Automatic entries are removed by deleting their source.'))
        else:
            audit.log(AuditLog.ACTION_DELETE, entry,
                      summary=_('Deleted ledger entry %(a)s') % {'a': entry.amount})
            entry.delete()
            messages.success(request, _('Entry deleted.'))
    return redirect('accounting:ledger')


@require_module(MODULE_ACCOUNTING)
def category_list(request):
    form = CategoryForm(request.POST or None)
    if request.method == 'POST' and request.user.can(MODULE_ACCOUNTING, 'create'):
        form.instance.tenant = request.tenant
        if form.is_valid():
            obj = form.save(commit=False)
            obj.tenant = request.tenant
            obj.save()
            audit.log(AuditLog.ACTION_CREATE, obj,
                      summary=_('Category %(n)s added') % {'n': obj.name})
            messages.success(request, _('Category added.'))
            return redirect('accounting:category_list')
    return render(request, 'accounting/categories.html', {
        'income': paginate(request, Category.objects.filter(
            tenant=request.tenant, kind=KIND_INCOME), 25, param='income_page'),
        'expense': paginate(request, Category.objects.filter(
            tenant=request.tenant, kind=KIND_EXPENSE), 25, param='expense_page'),
        'form': form,
        'can_create': request.user.can(MODULE_ACCOUNTING, 'create'),
    })


@admin_required
def category_toggle(request, pk):
    category = get_object_or_404(Category, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        category.is_active = not category.is_active
        category.save(update_fields=['is_active', 'updated_at'])
        audit.log(AuditLog.ACTION_UPDATE, category,
                  summary=_('Category %(n)s %(state)s') % {
                      'n': category.name,
                      'state': _('enabled') if category.is_active else _('disabled')})
    return redirect('accounting:category_list')


@admin_required
def recurring_list(request):
    form = RecurringExpenseForm(request.POST or None, tenant=request.tenant)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        obj.save()
        audit.log(AuditLog.ACTION_CREATE, obj,
                  summary=_('Recurring expense %(n)s added') % {'n': obj.name})
        messages.success(request, _('Recurring expense saved.'))
        return redirect('accounting:recurring_list')
    return render(request, 'accounting/recurring.html', {
        'rows': paginate(request, RecurringExpense.objects.filter(
            tenant=request.tenant).select_related('category', 'building'), 25),
        'form': form,
    })


@admin_required
def recurring_delete(request, pk):
    rule = get_object_or_404(RecurringExpense, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        audit.log(AuditLog.ACTION_DELETE, rule,
                  summary=_('Deleted recurring expense %(n)s') % {'n': rule.name})
        rule.delete()
        messages.success(request, _('Recurring expense deleted. Expenses already raised stay in the ledger.'))
    return redirect('accounting:recurring_list')


@admin_required
def recurring_run(request, pk):
    rule = get_object_or_404(RecurringExpense, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        entry = rule.generate()
        if entry:
            audit.log(AuditLog.ACTION_CREATE, entry,
                      summary=_('Recurring expense raised: %(n)s') % {'n': rule.name})
            messages.success(request, _('Expense raised for this month.'))
        else:
            messages.info(request, _('Already raised for this month, or not due yet.'))
    return redirect('accounting:recurring_list')
