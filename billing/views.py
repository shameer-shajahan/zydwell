"""Invoices, payments, receipts, POS printing and collection monitoring."""
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Exists, F, OuterRef, Q, Sum
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from core import audit
from core.constants import MODULE_BILLING, MODULE_POS
from core.filters import LocationFilter
from core.models import AuditLog, Status
from core.permissions import admin_required, require_module
from core.utils import date_range_from_request, paginate, redirect_back
from guests.models import Tenancy
from guests.services import with_deposit_held

from . import receipts, services, sharing
from .forms import (
    InvoiceEditForm, PaymentEditForm,
    DiscountForm, HandoverForm, InvoiceForm, LateFeeRuleForm, PaymentForm,
    PaymentModeForm, PrinterForm, RejectionForm, ReversalForm,
)
from .models import (
    CashHandover, Invoice, LateFeeRule, Payment, PaymentMode, PrinterConfig,
)


def _scoped_invoices(request):
    qs = Invoice.objects.filter(tenant=request.tenant).select_related(
        'tenancy__guest', 'tenancy__building', 'tenancy__floor', 'tenancy__apartment',
        'tenancy__room', 'tenancy__bed', 'status'
    )
    return request.user.scope_queryset(
        qs, building_field='tenancy__building', floor_field='tenancy__floor',
        apartment_field='tenancy__apartment')


def _scoped_payments(request):
    qs = Payment.objects.filter(tenant=request.tenant).select_related(
        'tenancy__guest', 'invoice', 'collected_by', 'mode'
    )
    return request.user.scope_queryset(
        qs, building_field='tenancy__building', floor_field='tenancy__floor',
        apartment_field='tenancy__apartment')


@require_module(MODULE_BILLING)
def invoice_list(request):
    location = LocationFilter(request)
    rows = location.apply(_scoped_invoices(request), prefix='tenancy__')
    status = request.GET.get('status', '')
    if status:
        rows = rows.filter(status__code=status)
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(Q(number__icontains=query) | Q(tenancy__guest__full_name__icontains=query)
                           | Q(tenancy__guest__mobile__icontains=query))
    totals = rows.aggregate(total=Sum('total'), paid=Sum('paid_amount'))
    return render(request, 'billing/invoice_list.html', {
        'page': paginate(request, rows, 30),
        'statuses': Status.objects.filter(tenant=request.tenant, kind=Status.KIND_INVOICE),
        'status': status, 'query': query, 'location': location,
        'sum_total': totals['total'] or Decimal('0'),
        'sum_paid': totals['paid'] or Decimal('0'),
        'can_create': request.user.can(MODULE_BILLING, 'create'),
    })


@require_module(MODULE_BILLING, 'create')
def invoice_create(request):
    initial = {}
    tenancy_id = request.GET.get('tenancy')
    if tenancy_id:
        tenancy = Tenancy.objects.filter(pk=tenancy_id, tenant=request.tenant).first()
        if tenancy:
            start, end = tenancy.period_for(timezone.localdate())
            initial = {'tenancy': tenancy.pk, 'period_start': start, 'period_end': end,
                       'due_date': start, 'rent_amount': tenancy.monthly_rent}
    form = InvoiceForm(request.POST or None, tenant=request.tenant, user=request.user,
                       initial=initial)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        extras = []
        if data.get('extra_amount') and data.get('extra_category'):
            extras.append({'category': data['extra_category'],
                           'description': data['extra_description'] or data['extra_category'].name,
                           'amount': data['extra_amount']})
        invoice = services.build_invoice(
            data['tenancy'], period_start=data['period_start'], period_end=data['period_end'],
            due_date=data['due_date'], user=request.user, rent_amount=data['rent_amount'],
            extras=extras, notes=data['notes'],
        )
        messages.success(request, _('Invoice %(no)s created.') % {'no': invoice.number})
        return redirect_back(request, 'billing:invoice_detail', pk=invoice.pk, made=invoice)
    return render(request, 'billing/invoice_form.html', {'form': form})


@require_module(MODULE_BILLING)
def invoice_detail(request, pk):
    invoice = get_object_or_404(_scoped_invoices(request), pk=pk)
    is_admin = request.user.is_admin or request.user.is_super_admin
    discount_form = DiscountForm(initial={'discount_amount': invoice.discount_amount,
                                          'discount_reason': invoice.discount_reason})
    if request.method == 'POST' and is_admin:
        discount_form = DiscountForm(request.POST)
        if discount_form.is_valid():
            before = audit.snapshot(invoice, ['discount_amount', 'total'])
            invoice.discount_amount = discount_form.cleaned_data['discount_amount']
            invoice.discount_reason = discount_form.cleaned_data['discount_reason']
            invoice.discount_approved_by = request.user
            invoice.save()
            invoice.recalculate()
            audit.log(AuditLog.ACTION_UPDATE, invoice,
                      summary=_('Discount approved on %(no)s: %(reason)s') % {
                          'no': invoice.number,
                          'reason': invoice.discount_reason},
                      changes=audit.diff(before, audit.snapshot(invoice,
                                                                ['discount_amount', 'total'])))
            messages.success(request, _('Discount applied.'))
            return redirect('billing:invoice_detail', pk=invoice.pk)

    return render(request, 'billing/invoice_detail.html', {
        'invoice': invoice,
        'lines': invoice.lines.select_related('category'),
        'payments': invoice.payments.select_related('collected_by'),
        'discount_form': discount_form,
        'is_admin': is_admin,
        'can_collect': request.user.can(MODULE_BILLING, 'create'),
        'credit': invoice.guest.advance_credit(),
    })


@admin_required
def invoice_edit(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk, tenant=request.tenant)
    tracked = InvoiceEditForm.Meta.fields + ['total']
    before = audit.snapshot(invoice, tracked)
    form = InvoiceEditForm(request.POST or None, instance=invoice)
    if request.method == 'POST' and form.is_valid():
        invoice = form.save(commit=False)
        if 'discount_amount' in form.changed_data or 'discount_reason' in form.changed_data:
            invoice.discount_approved_by = request.user
        invoice.save()
        invoice.recalculate()
        audit.log(AuditLog.ACTION_UPDATE, invoice,
                  summary=_('Edited invoice %(no)s') % {'no': invoice.number},
                  changes=audit.diff(before, audit.snapshot(invoice, tracked)))
        messages.success(request, _('Invoice updated.'))
        return redirect('billing:invoice_detail', pk=invoice.pk)
    return render(request, 'billing/edit_form.html', {
        'form': form,
        'heading': _('Edit invoice %(no)s') % {'no': invoice.number},
        'subheading': _('Extra charge lines stay as they are. Payments are not changed.'),
        'cancel_url': reverse('billing:invoice_detail', args=[invoice.pk]),
    })


@admin_required
def invoice_delete(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk, tenant=request.tenant)
    if request.method != 'POST':
        return redirect('billing:invoice_detail', pk=invoice.pk)
    done, reason = services.delete_invoice(invoice=invoice, user=request.user)
    if not done:
        messages.error(request, reason)
        return redirect('billing:invoice_detail', pk=invoice.pk)
    messages.success(request, _('Invoice deleted.'))
    return redirect('billing:invoice_list')


@require_module(MODULE_BILLING, 'create')
def apply_credit(request, pk):
    invoice = get_object_or_404(_scoped_invoices(request), pk=pk)
    if request.method == 'POST':
        payment = services.settle_from_credit(invoice=invoice, user=request.user)
        if payment:
            messages.success(request, _('Applied %(a)s from advance credit.') % {
                'a': payment.amount})
        else:
            messages.info(request, _('No advance credit available.'))
    return redirect('billing:invoice_detail', pk=invoice.pk)


@require_module(MODULE_BILLING)
def payment_list(request):
    location = LocationFilter(request)
    visible = location.apply(_scoped_payments(request), prefix='tenancy__')
    start, end = date_range_from_request(request)
    rows = visible.filter(paid_on__range=(start, end))
    collector = request.GET.get('collector', '')
    if collector:
        rows = rows.filter(collected_by_id=collector)
    mode = request.GET.get('mode', '')
    if mode:
        rows = rows.filter(mode_id=mode)
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(Q(receipt_number__icontains=query)
                           | Q(tenancy__guest__full_name__icontains=query))

    from accounts.models import User

    summary = services.collection_summary(request.tenant, start, end, payments=visible)
    return render(request, 'billing/payment_list.html', {
        'page': paginate(request, rows, 30),
        'start': start, 'end': end, 'query': query, 'mode': mode, 'collector': collector,
        'location': location,
        'summary': summary,
        'modes': PaymentMode.objects.filter(tenant=request.tenant, is_active=True),
        'collectors': User.objects.filter(tenant=request.tenant),
        'can_create': request.user.can(MODULE_BILLING, 'create'),
    })


@require_module(MODULE_BILLING, 'create')
def payment_create(request):
    initial = {}
    tenancy_id = request.GET.get('tenancy')
    invoice_id = request.GET.get('invoice')
    if invoice_id:
        invoice = Invoice.objects.filter(pk=invoice_id, tenant=request.tenant).first()
        if invoice:
            initial = {'invoice': invoice.pk, 'tenancy': invoice.tenancy_id,
                       'amount': invoice.balance}
    elif tenancy_id:
        initial = {'tenancy': tenancy_id}

    form = PaymentForm(request.POST or None, tenant=request.tenant, user=request.user,
                       initial=initial)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        payment = services.record_payment(
            tenant=request.tenant, user=request.user, tenancy=data['tenancy'],
            amount=data['amount'], mode=data['mode'], invoice=data['invoice'],
            reference=data['reference'], paid_on=data['paid_on'], note=data['note'],
            cheque_due_date=data['cheque_due_date'],
        )
        if payment.awaits_approval:
            messages.success(request, _(
                'Receipt %(no)s recorded. It is waiting for the owner to approve it.'
            ) % {'no': payment.receipt_number})
        else:
            messages.success(request, _('Receipt %(no)s recorded.') % {
                'no': payment.receipt_number})
        # "Save and print" asked for the receipt, so it wins over going back.
        if request.POST.get('and_print'):
            return redirect('billing:receipt', pk=payment.pk)
        return redirect_back(request, 'billing:payment_detail', pk=payment.pk, made=payment)
    return render(request, 'billing/payment_form.html', {'form': form})


@require_module(MODULE_BILLING)
def payment_detail(request, pk):
    payment = get_object_or_404(_scoped_payments(request), pk=pk)
    return render(request, 'billing/payment_detail.html', {
        'payment': payment,
        'reversal_form': ReversalForm(),
        'is_admin': request.user.is_admin or request.user.is_super_admin,
    })


@admin_required
def payment_reverse(request, pk):
    payment = get_object_or_404(Payment, pk=pk, tenant=request.tenant)
    form = ReversalForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        services.reverse_payment(payment=payment, user=request.user,
                                 reason=form.cleaned_data['reason'])
        messages.success(request, _('Payment reversed. The receipt number stays retired.'))
        return redirect('billing:payment_detail', pk=payment.pk)
    return render(request, 'billing/payment_reverse.html', {'payment': payment, 'form': form})


@admin_required
def payment_edit(request, pk):
    payment = get_object_or_404(Payment, pk=pk, tenant=request.tenant)
    tracked = PaymentEditForm.Meta.fields
    before = audit.snapshot(payment, tracked)
    form = PaymentEditForm(request.POST or None, instance=payment)
    if request.method == 'POST' and form.is_valid():
        payment = form.save()   # re-totals the invoice and the ledger line
        audit.log(AuditLog.ACTION_UPDATE, payment,
                  summary=_('Edited receipt %(no)s') % {'no': payment.receipt_number},
                  changes=audit.diff(before, audit.snapshot(payment, tracked)))
        messages.success(request, _('Payment updated.'))
        return redirect('billing:payment_detail', pk=payment.pk)
    return render(request, 'billing/edit_form.html', {
        'form': form,
        'heading': _('Edit receipt %(no)s') % {'no': payment.receipt_number},
        'subheading': _('%(guest)s · the receipt number stays the same.') % {
            'guest': payment.tenancy.guest.full_name},
        'cancel_url': reverse('billing:payment_detail', args=[payment.pk]),
    })


@admin_required
def payment_delete(request, pk):
    payment = get_object_or_404(Payment, pk=pk, tenant=request.tenant)
    if request.method != 'POST':
        return redirect('billing:payment_detail', pk=payment.pk)
    invoice_id = payment.invoice_id
    done, reason = services.delete_payment(payment=payment, user=request.user)
    if not done:
        messages.error(request, reason)
        return redirect('billing:payment_detail', pk=payment.pk)
    messages.success(request, _('Payment deleted.'))
    if invoice_id:
        return redirect('billing:invoice_detail', pk=invoice_id)
    return redirect('billing:payment_list')


@require_module(MODULE_BILLING)
def overdue(request):
    location = LocationFilter(request)
    today = timezone.localdate()
    invoices = location.apply(_scoped_invoices(request), prefix='tenancy__')
    items = services.owing(invoices)
    buckets = services.aging_buckets(request.tenant, invoices=invoices)

    # A guest is flagged once two or more of their invoices were settled late, or
    # are late right now.
    paid_late = Payment.objects.filter(
        invoice=OuterRef('pk'), is_reversed=False, paid_on__gt=OuterRef('due_date'))
    offenders = [
        {'guest_id': row['tenancy__guest_id'], 'name': row['tenancy__guest__full_name'],
         'times': row['times']}
        for row in services.with_balance(invoices).annotate(settled_late=Exists(paid_late))
        .filter(Q(due_balance__lte=0, settled_late=True)
                | Q(due_balance__gt=0, due_date__lt=today))
        .values('tenancy__guest_id', 'tenancy__guest__full_name')
        .annotate(times=Count('id')).filter(times__gte=2).order_by('-times')[:15]
    ]
    cheques = location.apply(_scoped_payments(request), prefix='tenancy__').filter(
        mode__needs_due_date=True, is_reversed=False, cheque_due_date__gte=today,
    ).order_by('cheque_due_date', 'pk')

    live = location.apply(request.user.scope_queryset(Tenancy.objects.filter(
        tenant=request.tenant, status__code__in=Tenancy.LIVE_STATUS_CODES)))
    shortfalls = with_deposit_held(live.select_related(
        'guest', 'building', 'floor', 'apartment', 'room', 'bed',
    )).annotate(shortfall=F('deposit_amount') - F('deposit_held_total')).filter(
        shortfall__gt=0).order_by('-shortfall', 'pk')

    return render(request, 'billing/overdue.html', {
        'items': paginate(request, items, 30),
        'buckets': buckets,
        'aging_rows': [(label, buckets[label]) for label in
                       ('current', '0-30', '31-60', '60+')],
        'recent_due': buckets['0-30'],
        'total_due': items.aggregate(s=Sum('due_balance'))['s'] or Decimal('0'),
        'cheques': paginate(request, cheques, 10, param='cheque_page'),
        'shortfalls': paginate(request, shortfalls, 10, param='shortfall_page'),
        'offenders': offenders,
        'location': location,
    })


# --- Receipts & POS ---------------------------------------------------
@require_module(MODULE_BILLING)
def receipt_view(request, pk):
    payment = get_object_or_404(_scoped_payments(request), pk=pk)
    printer = PrinterConfig.objects.filter(tenant=request.tenant, is_default=True).first() \
        or PrinterConfig.objects.filter(tenant=request.tenant).first()
    return render(request, 'billing/receipt.html', {
        'payment': payment,
        'public_url': sharing.receipt_url(payment, request),
        'link_days': settings.ZYDWELL['RECEIPT_LINK_DAYS'],
        'printer': printer,
        'text': receipts.render_text(payment, printer),
        'printers': PrinterConfig.objects.filter(tenant=request.tenant),
        'can_print': request.user.can(MODULE_POS, 'view'),
    })


@require_module(MODULE_POS, 'view')
def receipt_print(request, pk):
    payment = get_object_or_404(_scoped_payments(request), pk=pk)
    printer = PrinterConfig.objects.filter(
        pk=request.POST.get('printer'), tenant=request.tenant
    ).first() or PrinterConfig.objects.filter(tenant=request.tenant, is_default=True).first()
    copies = int(request.POST.get('copies') or (printer.copies if printer else 1))

    payload = receipts.render_escpos(payment, printer)
    ok, note = True, ''
    for index in range(max(copies, 1)):
        ok, note = receipts.send_to_printer(printer, payload)
        if not ok:
            break
    audit.log(AuditLog.ACTION_PRINT, payment,
              summary=_('Receipt %(no)s sent to %(printer)s: %(note)s') % {
                  'no': payment.receipt_number, 'printer': printer or '—', 'note': note})
    if ok:
        messages.success(request, _('Sent to the printer. %(note)s') % {'note': note})
    else:
        messages.error(request, _('%(note)s Use the PDF fallback instead.') % {'note': note})
    return redirect('billing:receipt', pk=payment.pk)


@require_module(MODULE_BILLING)
def receipt_escpos(request, pk):
    """Raw ESC/POS bytes for the local print agent to relay."""
    payment = get_object_or_404(_scoped_payments(request), pk=pk)
    printer = PrinterConfig.objects.filter(tenant=request.tenant, is_default=True).first()
    payload = receipts.render_escpos(payment, printer)
    response = HttpResponse(payload, content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{payment.receipt_number}.bin"'
    return response


@require_module(MODULE_BILLING)
def receipt_whatsapp(request, pk):
    """Send the guest their receipt: a message carrying a link to the PDF."""
    payment = get_object_or_404(_scoped_payments(request), pk=pk)
    guest = payment.tenancy.guest
    url = sharing.receipt_url(payment, request)

    from messaging import services as messaging

    context = messaging.base_context(
        request.tenant, guest=guest, tenancy=payment.tenancy, payment=payment,
        extra={'link': url, 'days': str(settings.ZYDWELL['RECEIPT_LINK_DAYS'])},
    )
    fallback = _(
        'Thank you %(guest)s. We received %(currency)s %(amount)s for %(bed)s. '
        'Receipt %(receipt)s: %(link)s'
    ) % {'guest': guest.full_name, 'currency': request.tenant.currency,
         'amount': f'{payment.amount:,.2f}', 'bed': payment.tenancy.location_label(),
         'receipt': payment.receipt_number, 'link': url}

    entry, chat = messaging.hand_to_whatsapp(
        tenant=request.tenant, template_code='receipt_share',
        to_number=guest.messaging_number(), context=context, guest=guest,
        tenancy=payment.tenancy, invoice=payment.invoice, user=request.user,
        body_override=fallback, prefer_web=messaging.prefers_whatsapp_web(request),
    )
    audit.log(AuditLog.ACTION_PRINT, payment,
              summary=_('Shared receipt %(no)s with %(name)s on WhatsApp') % {
                  'no': payment.receipt_number, 'name': guest.full_name})
    if not chat:
        messages.error(request, entry.error or _('No WhatsApp number for this guest.'))
        return redirect('billing:receipt', pk=payment.pk)
    return redirect(chat)


def public_receipt(request, token):
    """The guest's own receipt, to read on screen. Nothing to download, no login."""
    payment, problem = sharing.payment_from_token(token)
    if payment is None:
        return render(request, 'billing/receipt_link_dead.html',
                      {'expired': problem == 'expired', 'org_name': 'ZyDwell'},
                      status=410 if problem == 'expired' else 404)

    response = render(request, 'billing/public_receipt.html',
                      receipt_view_context(payment))
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def receipt_view_context(payment):
    """What a guest may see on a receipt — their own payment, nothing around it."""
    invoice = payment.invoice
    return {
        'payment': payment,
        'invoice': invoice,
        'tenancy': payment.tenancy,
        'guest': payment.tenancy.guest,
        'building': payment.tenancy.building,
        'org_name': payment.tenant.name,
        'org_currency': f'{payment.tenant.currency} ',
        'lines': invoice.lines.select_related('category') if invoice else [],
        'balance': invoice.balance if invoice else None,
    }


@require_module(MODULE_POS)
def printer_list(request):
    return render(request, 'billing/printer_list.html', {
        'printers': paginate(request, PrinterConfig.objects.filter(tenant=request.tenant), 25),
        'can_edit': request.user.can(MODULE_POS, 'edit') or request.user.is_admin,
    })


@admin_required
def printer_form(request, pk=None):
    printer = get_object_or_404(PrinterConfig, pk=pk, tenant=request.tenant) if pk else None
    form = PrinterForm(request.POST or None, instance=printer)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        obj.save()
        audit.log(AuditLog.ACTION_UPDATE if printer else AuditLog.ACTION_CREATE, obj,
                  summary=_('Printer %(n)s saved') % {'n': obj.name})
        messages.success(request, _('Printer saved.'))
        return redirect('billing:printer_list')
    return render(request, 'billing/printer_form.html', {'form': form, 'printer': printer})


@require_module(MODULE_POS)
def printer_test(request, pk):
    printer = get_object_or_404(PrinterConfig, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        sample = (
            receipts.INIT + receipts.ALIGN_CENTER + receipts.BOLD_ON
            + _('ZyDwell test print').encode('utf-8') + b'\n' + receipts.BOLD_OFF
            + f'{printer.name} · {printer.paper_width}mm\n'.encode('utf-8')
            + timezone.localtime().strftime('%d-%m-%Y %H:%M:%S\n').encode('utf-8')
            + b'-' * printer.columns + b'\n' + b'\n\n' + receipts.CUT
        )
        ok, note = receipts.send_to_printer(printer, sample)
        printer.last_test_at = timezone.now()
        printer.last_test_ok = ok
        printer.last_test_message = note[:200]
        printer.save(update_fields=['last_test_at', 'last_test_ok', 'last_test_message'])
        (messages.success if ok else messages.error)(request, note)
    return redirect('billing:printer_list')


# --- Cash control -----------------------------------------------------
@require_module(MODULE_BILLING)
def daily_collection(request):
    start, end = date_range_from_request(request, default_days=0)
    staff_id = request.GET.get('staff') or (
        request.user.pk if request.user.is_staff_member else None
    )
    from accounts.models import User

    staff = User.objects.filter(pk=staff_id, tenant=request.tenant).first() if staff_id else None
    summary = services.collection_summary(request.tenant, start, end, staff=staff)
    handover = CashHandover.objects.filter(
        tenant=request.tenant, staff=staff or request.user, handover_date=end
    ).first()
    form = HandoverForm(instance=handover, initial={
        'handover_date': end, 'declared_amount': summary['cash']})
    if request.method == 'POST' and request.user.can(MODULE_BILLING, 'create'):
        form = HandoverForm(request.POST, instance=handover)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.tenant = request.tenant
            obj.staff = staff or request.user
            obj.expected_amount = summary['cash']
            obj.save()
            audit.log(AuditLog.ACTION_CREATE, obj,
                      summary=_('Cash handover declared: %(a)s') % {'a': obj.declared_amount})
            messages.success(request, _('Handover recorded. The admin will confirm it.'))
            return redirect('billing:daily_collection')

    return render(request, 'billing/daily_collection.html', {
        'summary': summary, 'start': start, 'end': end, 'staff': staff,
        'staff_list': User.objects.filter(tenant=request.tenant),
        'form': form, 'handover': handover,
        'is_admin': request.user.is_admin or request.user.is_super_admin,
        'pending': CashHandover.objects.filter(
            tenant=request.tenant, confirmed_at__isnull=True
        ).select_related('staff'),
    })


@admin_required
def handover_confirm(request, pk):
    handover = get_object_or_404(CashHandover, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        handover.confirmed_by = request.user
        handover.confirmed_at = timezone.now()
        handover.save(update_fields=['confirmed_by', 'confirmed_at'])
        audit.log(AuditLog.ACTION_UPDATE, handover,
                  summary=_('Confirmed cash handover from %(who)s') % {
                      'who': handover.staff.display_name()})
        messages.success(request, _('Handover confirmed.'))
    return redirect('billing:daily_collection')


@admin_required
def approvals(request):
    """Money a collector took in, waiting for the owner to confirm it."""
    from guests.models import DepositTransaction

    payments = Payment.objects.filter(
        tenant=request.tenant, approval=Payment.APPROVAL_PENDING
    ).select_related('tenancy__guest', 'tenancy__building', 'collected_by', 'mode',
                     'invoice')
    deposits = DepositTransaction.objects.filter(
        tenant=request.tenant, approval='pending'
    ).select_related('tenancy__guest', 'tenancy__building', 'recorded_by', 'mode')

    return render(request, 'billing/approvals.html', {
        'payments': paginate(request, payments, 25),
        'deposits': paginate(request, deposits, 25, param='deposit_page'),
        'total': sum((p.amount for p in payments), Decimal('0')),
        'deposit_total': sum((d.amount for d in deposits), Decimal('0')),
        'reject_form': RejectionForm(),
    })


@admin_required
def payment_decide(request, pk, verdict):
    payment = get_object_or_404(Payment, pk=pk, tenant=request.tenant)
    if request.method != 'POST' or not payment.awaits_approval:
        return redirect('billing:approvals')

    if verdict == 'approve':
        services.approve_payment(payment=payment, user=request.user,
                                 note=request.POST.get('note', ''))
        messages.success(request, _('Receipt %(no)s approved.') % {
            'no': payment.receipt_number})
    else:
        form = RejectionForm(request.POST)
        if not form.is_valid():
            messages.error(request, _('Say why it is being rejected.'))
            return redirect('billing:approvals')
        services.reject_payment(payment=payment, user=request.user,
                                reason=form.cleaned_data['reason'])
        messages.warning(request, _(
            'Receipt %(no)s rejected and reversed. The guest still owes it.'
        ) % {'no': payment.receipt_number})
    return redirect('billing:approvals')


@admin_required
def deposit_decide(request, pk, verdict):
    from guests.models import DepositTransaction
    from guests.services import approve_deposit, reject_deposit

    entry = get_object_or_404(DepositTransaction, pk=pk, tenant=request.tenant)
    if request.method != 'POST' or not entry.awaits_approval:
        return redirect('billing:approvals')

    if verdict == 'approve':
        approve_deposit(entry=entry, user=request.user,
                        note=request.POST.get('note', ''))
        messages.success(request, _('Deposit %(no)s approved.') % {
            'no': entry.receipt_number})
    else:
        form = RejectionForm(request.POST)
        if not form.is_valid():
            messages.error(request, _('Say why it is being rejected.'))
            return redirect('billing:approvals')
        reject_deposit(entry=entry, user=request.user,
                       reason=form.cleaned_data['reason'])
        messages.warning(request, _('Deposit %(no)s rejected and removed.') % {
            'no': entry.receipt_number})
    return redirect('billing:approvals')


@admin_required
def payment_mode_list(request):
    form = PaymentModeForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        if PaymentMode.objects.filter(tenant=request.tenant, code=obj.code).exists():
            messages.error(request, _('That code is already in use.'))
        else:
            obj.save()
            audit.log(AuditLog.ACTION_CREATE, obj,
                      summary=_('Payment mode %(n)s added') % {'n': obj.name})
            messages.success(request, _('Payment mode added.'))
            return redirect('billing:payment_mode_list')
    page = paginate(request, PaymentMode.objects.filter(tenant=request.tenant), 25)
    return render(request, 'billing/payment_modes.html', {
        'rows': [{'obj': mode, 'in_use': mode.in_use()} for mode in page],
        'page': page,
        'form': form,
    })


@admin_required
def payment_mode_edit(request, pk):
    mode = get_object_or_404(PaymentMode, pk=pk, tenant=request.tenant)
    before = audit.snapshot(mode)
    form = PaymentModeForm(request.POST or None, instance=mode)
    if request.method == 'POST' and form.is_valid():
        obj = form.save()
        audit.log(AuditLog.ACTION_UPDATE, obj,
                  summary=_('Payment mode %(n)s updated') % {'n': obj.name},
                  changes=audit.diff(before, audit.snapshot(obj)))
        messages.success(request, _('Payment mode saved.'))
        return redirect('billing:payment_mode_list')
    return render(request, 'billing/payment_mode_form.html', {'form': form, 'mode': mode})


@admin_required
def payment_mode_delete(request, pk):
    mode = get_object_or_404(PaymentMode, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        if mode.in_use():
            mode.is_active = False
            mode.save(update_fields=['is_active', 'updated_at'])
            messages.warning(request, _(
                'Money has already been taken this way, so it has been switched off '
                'rather than deleted.'))
        else:
            audit.log(AuditLog.ACTION_DELETE, mode,
                      summary=_('Payment mode %(n)s deleted') % {'n': mode.name})
            mode.delete()
            messages.success(request, _('Payment mode removed.'))
    return redirect('billing:payment_mode_list')


@admin_required
def late_fee_rule(request):
    rule = LateFeeRule.objects.filter(tenant=request.tenant).first()
    form = LateFeeRuleForm(request.POST or None, instance=rule)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        obj.save()
        audit.log(AuditLog.ACTION_UPDATE, obj, summary=_('Late fee rule updated'))
        messages.success(request, _('Late fee rule saved.'))
        return redirect('billing:late_fee_rule')
    return render(request, 'billing/late_fee_rule.html', {'form': form, 'rule': rule})


@admin_required
def run_billing(request):
    """Raise today's invoices on demand, without waiting for the nightly job."""
    if request.method == 'POST':
        created = services.generate_due_invoices(request.tenant, user=request.user)
        fees = services.apply_late_fees(request.tenant)
        messages.success(request, _(
            '%(n)d invoice(s) raised, %(f)d late fee(s) updated.'
        ) % {'n': len(created), 'f': len(fees)})
    return redirect('billing:invoice_list')
