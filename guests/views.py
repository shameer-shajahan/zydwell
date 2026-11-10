"""Guest records, documents and the tenancy lifecycle."""
from decimal import Decimal

from django.contrib import messages
from django.conf import settings
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from core import audit
from core.constants import MODULE_BILLING, MODULE_GUESTS, MODULE_POS
from core.filters import LocationFilter
from core.models import AuditLog, Status
from core.permissions import admin_required, require_module
from core.utils import paginate, redirect_back

from . import services
from .forms import (
    CheckInForm, CheckOutForm, DepositForm, GuestDocumentForm, GuestForm,
    GuestSelfProfileForm, NoticeForm, ShareProfileLinkForm, TenancyEditForm, TransferForm,
)
from .models import DepositTransaction, Guest, GuestDocument, GuestInvite, Tenancy


def _scoped_tenancies(request):
    qs = Tenancy.objects.filter(tenant=request.tenant).select_related(
        'guest', 'bed', 'room', 'apartment', 'floor', 'building', 'status'
    )
    return request.user.scope_queryset(qs)


def _live_stays():
    return Tenancy.objects.filter(status__code__in=Tenancy.LIVE_STATUS_CODES)


@require_module(MODULE_GUESTS)
def guest_list(request):
    location = LocationFilter(request)
    rows = Guest.objects.filter(tenant=request.tenant)
    query = request.GET.get('q', '').strip()
    if query:
        # Subqueries rather than joins: no duplicate rows, so no DISTINCT.
        rows = rows.filter(
            Q(full_name__icontains=query) | Q(mobile__icontains=query)
            | Q(whatsapp_number__icontains=query) | Q(employer__icontains=query)
            | Q(pk__in=GuestDocument.objects.filter(
                tenant=request.tenant, number__icontains=query).values('guest_id'))
        )
    state = request.GET.get('state', '')
    live = _live_stays().filter(tenant=request.tenant)
    if state == 'current':
        rows = rows.filter(pk__in=live.values('guest_id'))
    elif state == 'past':
        rows = rows.exclude(pk__in=live.values('guest_id'))
    if location.active:
        # Who lives there now — or, looking at past guests, who once did.
        stays = location.apply(live if state == 'current' else
                               Tenancy.objects.filter(tenant=request.tenant))
        rows = rows.filter(pk__in=stays.values('guest_id'))

    horizon = timezone.localdate() + timezone.timedelta(days=30)
    rows = rows.annotate(expiring_count=Count('documents', filter=Q(
        documents__expiry_date__isnull=False, documents__expiry_date__lte=horizon),
    )).order_by('full_name', 'pk')    # GROUP BY drops Meta.ordering
    rows = rows.prefetch_related(Prefetch(
        'tenancies', to_attr='live_stays',
        queryset=_live_stays().select_related(
            'bed', 'room', 'apartment', 'floor', 'building')))

    page = paginate(request, rows)
    cards = [{'obj': guest,
              'tenancy': guest.live_stays[0] if guest.live_stays else None,
              'expiring': guest.expiring_count} for guest in page]
    return render(request, 'guests/guest_list.html', {
        'page': page, 'cards': cards, 'query': query, 'state': state,
        'location': location,
        'can_edit': request.user.can(MODULE_GUESTS, 'create'),
    })


@require_module(MODULE_GUESTS, 'create')
def guest_form(request, pk=None):
    guest = get_object_or_404(Guest, pk=pk, tenant=request.tenant) if pk else None
    before = audit.snapshot(guest) if guest else {}
    form = GuestForm(request.POST or None, request.FILES or None, instance=guest)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        obj.save()
        audit.log(AuditLog.ACTION_UPDATE if guest else AuditLog.ACTION_CREATE, obj,
                  summary=_('Guest %(name)s saved') % {'name': obj.full_name},
                  changes=audit.diff(before, audit.snapshot(obj)) if guest else {})
        messages.success(request, _('Guest saved.'))
        return redirect_back(request, 'guests:guest_detail', pk=obj.pk, made=obj)
    return render(request, 'guests/guest_form.html', {'form': form, 'guest': guest})


@require_module(MODULE_GUESTS)
def guest_detail(request, pk):
    guest = get_object_or_404(Guest, pk=pk, tenant=request.tenant)
    doc_form = GuestDocumentForm()
    if request.method == 'POST' and request.user.can(MODULE_GUESTS, 'create'):
        doc_form = GuestDocumentForm(request.POST, request.FILES)
        if doc_form.is_valid():
            doc = doc_form.save(commit=False)
            doc.guest = guest
            doc.tenant = request.tenant
            doc.save()
            audit.log(AuditLog.ACTION_CREATE, doc,
                      summary=_('Document added for %(name)s') % {'name': guest.full_name})
            messages.success(request, _('Document saved.'))
            return redirect('guests:guest_detail', pk=guest.pk)

    from billing.models import Invoice, Payment

    invite = GuestInvite.objects.filter(
        guest=guest, revoked_at__isnull=True).order_by('-created_at').first()

    return render(request, 'guests/guest_detail.html', {
        'guest': guest,
        'invite': invite,
        'invite_url': invite.build_url(request) if invite and invite.is_usable else '',
        'share_form': ShareProfileLinkForm(),
        'current': guest.current_tenancy(),
        'tenancies': paginate(request, guest.tenancies.select_related(
            'room', 'bed', 'status', 'building'), 10, param='stay_page'),
        'documents': guest.documents.all(),
        'doc_form': doc_form,
        'invoices': paginate(request, Invoice.objects.filter(
            tenancy__guest=guest).select_related('status'), 12, param='invoice_page'),
        'payments': Payment.objects.filter(tenancy__guest=guest).select_related('collected_by')[:12],
        'outstanding': guest.outstanding_balance(),
        'credit': guest.advance_credit(),
        'can_edit': request.user.can(MODULE_GUESTS, 'edit'),
        'can_bill': request.user.can(MODULE_BILLING, 'view'),
    })


@admin_required
def guest_delete(request, pk):
    """Erase a guest and everything attached to them. Admin only, no undo."""
    guest = get_object_or_404(Guest, pk=pk, tenant=request.tenant)
    footprint = services.guest_footprint(guest)

    if request.method == 'POST':
        if request.POST.get('confirm_name', '').strip().casefold() != \
                guest.full_name.strip().casefold():
            messages.error(request, _(
                'Type the guest\u2019s full name exactly to confirm the deletion.'))
        else:
            name = guest.full_name
            removed = services.purge_guest(guest=guest, user=request.user)
            detail = ', '.join(f'{count} {what}' for what, count in removed.items())
            messages.success(request, _('%(name)s was deleted permanently. %(detail)s') % {
                'name': name, 'detail': detail or _('There were no linked records.')})
            return redirect('guests:guest_list')

    return render(request, 'guests/guest_delete.html', {
        'guest': guest,
        'footprint': footprint,
    })


@require_module(MODULE_GUESTS, 'edit')
def document_delete(request, pk):
    doc = get_object_or_404(GuestDocument, pk=pk, tenant=request.tenant)
    guest_id = doc.guest_id
    if request.method == 'POST':
        audit.log(AuditLog.ACTION_DELETE, doc, summary=_('Deleted document %(n)s') % {'n': doc})
        doc.delete()
        messages.success(request, _('Document removed.'))
    return redirect('guests:guest_detail', pk=guest_id)


@require_module(MODULE_GUESTS)
def expiring_documents(request):
    location = LocationFilter(request)
    days = request.GET.get('days', '30')
    days = int(days) if days.isdigit() else 30
    horizon = timezone.localdate() + timezone.timedelta(days=days)
    rows = GuestDocument.objects.filter(
        tenant=request.tenant, expiry_date__isnull=False, expiry_date__lte=horizon
    ).select_related('guest')
    if location.active:
        stays = location.apply(_live_stays().filter(tenant=request.tenant))
        rows = rows.filter(guest_id__in=stays.values('guest_id'))
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(Q(guest__full_name__icontains=query) | Q(guest__mobile__icontains=query)
                           | Q(number__icontains=query))
    return render(request, 'guests/expiring_documents.html', {
        'page': paginate(request, rows, 40),
        'days': str(days), 'query': query, 'location': location,
        'today': timezone.localdate(),
    })


@require_module(MODULE_GUESTS)
def tenancy_list(request):
    location = LocationFilter(request)
    rows = location.apply(_scoped_tenancies(request))
    state = request.GET.get('state', 'live')
    if state == 'live':
        rows = rows.filter(status__code__in=Tenancy.LIVE_STATUS_CODES)
    elif state == 'vacated':
        rows = rows.filter(status__code='vacated')
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(Q(guest__full_name__icontains=query)
                           | Q(guest__mobile__icontains=query)
                           | Q(room__number__icontains=query))
    return render(request, 'guests/tenancy_list.html', {
        'page': paginate(request, services.with_outstanding(rows)),
        'state': state, 'query': query, 'location': location,
    })


@require_module(MODULE_GUESTS)
def tenancy_detail(request, pk):
    tenancy = get_object_or_404(_scoped_tenancies(request), pk=pk)
    from billing.models import Invoice, Payment

    return render(request, 'guests/tenancy_detail.html', {
        'tenancy': tenancy,
        'invoices': paginate(request, Invoice.objects.filter(
            tenancy=tenancy).select_related('status'), 12, param='invoice_page'),
        'payments': paginate(request, Payment.objects.filter(
            tenancy=tenancy).select_related('collected_by'), 12, param='payment_page'),
        'deposits': paginate(request, tenancy.deposit_transactions.all(), 12,
                             param='deposit_page'),
        'transfers': paginate(request, tenancy.transfers.select_related(
            'from_bed', 'to_bed'), 12, param='transfer_page'),
        'deposit_held': tenancy.deposit_held(),
        'deposit_shortfall': tenancy.deposit_shortfall(),
        'outstanding': tenancy.outstanding(),
        'deposit_form': DepositForm(tenant=request.tenant),
        'can_edit': request.user.can(MODULE_GUESTS, 'edit'),
        'can_bill': request.user.can(MODULE_BILLING, 'create'),
        'is_admin': request.user.is_admin or request.user.is_super_admin,
    })


@require_module(MODULE_GUESTS, 'create')
def check_in(request):
    initial = {}
    bed_id = request.GET.get('bed')
    if bed_id:
        from properties.models import Bed

        bed = Bed.objects.filter(pk=bed_id, tenant=request.tenant).select_related('room').first()
        if bed:
            initial = {'bed': bed.pk, 'room': bed.room_id, 'monthly_rent': bed.monthly_price}
    if request.GET.get('guest'):
        initial['guest'] = request.GET['guest']

    form = CheckInForm(request.POST or None, tenant=request.tenant, user=request.user,
                       initial=initial)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        prorate, first_amount = form.billing_choices()
        tenancy = services.check_in(
            tenant=request.tenant, user=request.user, guest=data['guest'], room=data['room'],
            bed=data['bed'], check_in_date=data['check_in_date'],
            monthly_rent=data['monthly_rent'], deposit_amount=data['deposit_amount'],
            deposit_received=data['deposit_received'] or Decimal('0'),
            billing_day=data['billing_day'], billing_cycle=data['billing_cycle'],
            expected_check_out=data['expected_check_out'],
            agreement_note=data['agreement_note'],
            rent_mode=data['rent_mode'],
            prorate_first_period=prorate,
            first_invoice_amount=first_amount,
        )
        if data['raise_first_invoice']:
            _raise_first_invoice(request, tenancy)
        messages.success(request, _('%(name)s checked in.') % {'name': tenancy.guest.full_name})
        return redirect_back(request, 'guests:tenancy_detail', pk=tenancy.pk, made=tenancy)
    return render(request, 'guests/check_in.html', {'form': form})


def _raise_first_invoice(request, tenancy):
    from billing.services import build_invoice

    period_start, period_end = tenancy.period_for(tenancy.check_in_date)
    start = max(period_start, tenancy.check_in_date)
    # Only split the month when the agreement says to; plenty of owners take the
    # whole month's rent however late in the month the guest arrived.
    prorata = start > period_start and tenancy.prorate_first_period
    invoice = build_invoice(
        tenancy, period_start=start, period_end=period_end,
        due_date=tenancy.due_date_for(start, period_end), user=request.user,
        rent_amount=tenancy.first_invoice_amount, prorata=prorata,
        period_days=tenancy.period_length(period_start),
    )
    messages.info(request, _('Invoice %(no)s raised for %(amount)s.') % {
        'no': invoice.number, 'amount': invoice.total})
    return invoice


@require_module(MODULE_GUESTS, 'edit')
def check_out(request, pk):
    tenancy = get_object_or_404(_scoped_tenancies(request), pk=pk)
    form = CheckOutForm(request.POST or None, tenancy=tenancy)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        deductions = []
        if data.get('deduction_amount'):
            deductions.append({'amount': data['deduction_amount'],
                               'reason': data['deduction_reason']})
        services.check_out(
            tenancy=tenancy, user=request.user, check_out_date=data['check_out_date'],
            deductions=deductions, refund_amount=data.get('refund_amount') or Decimal('0'),
            note=data.get('note', ''),
        )
        messages.success(request, _('%(name)s checked out.') % {'name': tenancy.guest.full_name})
        return redirect('guests:tenancy_detail', pk=tenancy.pk)
    return render(request, 'guests/check_out.html', {
        'form': form, 'tenancy': tenancy,
        'deposit_held': tenancy.deposit_held(),
        'outstanding': tenancy.outstanding(),
    })


@require_module(MODULE_GUESTS, 'edit')
def transfer(request, pk):
    tenancy = get_object_or_404(_scoped_tenancies(request), pk=pk)
    form = TransferForm(request.POST or None, tenancy=tenancy, user=request.user)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        record = services.transfer_bed(
            tenancy=tenancy, user=request.user, new_bed=data['new_bed'],
            new_room=data['new_room'], transfer_date=data['transfer_date'],
            new_rent=data['new_rent'], reason=data['reason'],
        )
        messages.success(request, _(
            'Transferred. Pro-rata: %(old)s on the old bed, %(new)s on the new one.'
        ) % {'old': record.old_prorata, 'new': record.new_prorata})
        return redirect('guests:tenancy_detail', pk=tenancy.pk)
    return render(request, 'guests/transfer.html', {'form': form, 'tenancy': tenancy})


@require_module(MODULE_GUESTS, 'edit')
def give_notice(request, pk):
    tenancy = get_object_or_404(_scoped_tenancies(request), pk=pk)
    form = NoticeForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        services.give_notice(tenancy=tenancy, user=request.user,
                             notice_date=form.cleaned_data['notice_given_on'],
                             expected_check_out=form.cleaned_data['expected_check_out'])
        messages.success(request, _('Notice recorded.'))
        return redirect('guests:tenancy_detail', pk=tenancy.pk)
    return render(request, 'guests/notice.html', {'form': form, 'tenancy': tenancy})


@require_module(MODULE_GUESTS, 'edit')
def tenancy_edit(request, pk):
    tenancy = get_object_or_404(_scoped_tenancies(request), pk=pk)
    is_admin = request.user.is_admin or request.user.is_super_admin
    # Snapshot before the form binds — a ModelForm mutates its instance in is_valid().
    audited = ['monthly_rent', 'billing_day', 'deposit_amount', 'expected_check_out',
               'billing_cycle', 'rent_mode', 'prorate_first_period',
               'first_invoice_amount']
    before = audit.snapshot(tenancy, audited)
    form = TenancyEditForm(request.POST or None, instance=tenancy)
    if not is_admin:
        form.fields['monthly_rent'].disabled = True
        form.fields['monthly_rent'].help_text = _('Only the building owner may change the rent.')
    if request.method == 'POST' and form.is_valid():
        obj = form.save()
        changes = audit.diff(before, audit.snapshot(obj, audited))
        audit.log(AuditLog.ACTION_UPDATE, obj,
                  summary=_('Tenancy updated for %(name)s') % {'name': obj.guest.full_name},
                  changes=changes)
        messages.success(request, _('Tenancy updated.'))
        return redirect('guests:tenancy_detail', pk=obj.pk)
    return render(request, 'guests/tenancy_edit.html', {'form': form, 'tenancy': tenancy})


@require_module(MODULE_GUESTS, 'edit')
def add_deposit(request, pk):
    tenancy = get_object_or_404(_scoped_tenancies(request), pk=pk)
    if request.method == 'POST':
        form = DepositForm(request.POST, tenant=request.tenant)
        if form.is_valid():
            entry = services.record_deposit(
                tenancy=tenancy, user=request.user, amount=form.cleaned_data['amount'],
                reason=form.cleaned_data['reason'], mode=form.cleaned_data['mode'],
                reference=form.cleaned_data['reference'])
            if entry.awaits_approval:
                messages.success(request, _(
                    'Deposit recorded. It is waiting for the owner to approve it.'))
            else:
                messages.success(request, _('Deposit recorded.'))
        else:
            messages.error(request, _('Enter a valid amount.'))
    return redirect('guests:tenancy_detail', pk=tenancy.pk)


@require_module(MODULE_GUESTS, 'edit')
def share_profile_link(request, pk):
    """Issue a link the guest can use to fill in their own profile."""
    guest = get_object_or_404(Guest, pk=pk, tenant=request.tenant)
    form = ShareProfileLinkForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        invite = GuestInvite.issue(guest=guest, user=request.user,
                                   days=int(form.cleaned_data['days']))
        audit.log(AuditLog.ACTION_CREATE, invite,
                  summary=_('Profile link issued for %(name)s, valid %(days)s days') % {
                      'name': guest.full_name, 'days': form.cleaned_data['days']})
        if request.POST.get('and_send'):
            return redirect(f"{reverse('guests:send_profile_link', args=[invite.pk])}")
        messages.success(request, _('Link created. Copy it, or send it on WhatsApp.'))
    return redirect('guests:guest_detail', pk=guest.pk)


@require_module(MODULE_GUESTS, 'edit')
def send_profile_link(request, pk):
    """Send the live link to the guest over WhatsApp."""
    invite = get_object_or_404(
        GuestInvite.objects.select_related('guest'), pk=pk, tenant=request.tenant)
    guest = invite.guest
    tenancy = guest.current_tenancy()
    url = invite.build_url(request)

    from messaging import services as messaging

    context = messaging.base_context(
        request.tenant, guest=guest, tenancy=tenancy,
        extra={'link': url, 'days': str(invite.days_left)},
    )
    fallback = _(
        'Hello %(guest)s, please complete your details for %(building)s here: %(link)s '
        'The link works for %(days)s more day(s).'
    ) % {'guest': guest.full_name,
         'building': tenancy.building.name if tenancy else request.tenant.name,
         'link': url, 'days': invite.days_left}

    entry, chat = messaging.hand_to_whatsapp(
        tenant=request.tenant, template_code='profile_link',
        to_number=guest.messaging_number(), context=context, guest=guest,
        tenancy=tenancy, user=request.user, body_override=fallback,
        prefer_web=messaging.prefers_whatsapp_web(request),
    )
    audit.log(AuditLog.ACTION_CREATE, entry,
              summary=_('Opened the profile link for %(name)s in WhatsApp') % {
                  'name': guest.full_name})
    if not chat:
        messages.error(request, entry.error or _('No WhatsApp number for this guest.'))
        return redirect('guests:guest_detail', pk=guest.pk)
    return redirect(chat)


@require_module(MODULE_GUESTS, 'edit')
def revoke_profile_link(request, pk):
    invite = get_object_or_404(GuestInvite, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        invite.revoked_at = timezone.now()
        invite.save(update_fields=['revoked_at', 'updated_at'])
        audit.log(AuditLog.ACTION_UPDATE, invite,
                  summary=_('Profile link revoked for %(name)s') % {
                      'name': invite.guest.full_name})
        messages.success(request, _('That link no longer works.'))
    return redirect('guests:guest_detail', pk=invite.guest_id)


def _public_context(invite=None, **extra):
    """Shared shell context. public_base.html uses org_name as a filter
    argument, and a missing filter argument raises rather than falling back."""
    tenancy = invite.guest.current_tenancy() if invite else None
    context = {
        'org_name': invite.tenant.name if invite else 'ZyDwell',
        'building': tenancy.building if tenancy else None,
        'tenancy': tenancy,
        'invite': invite,
        'guest': invite.guest if invite else None,
        'org_currency': f'{invite.tenant.currency} ' if invite else '',
    }
    context.update(extra)
    return context


def _live_invite(token):
    return GuestInvite.objects.filter(token=token).select_related('guest', 'tenant').first()


def _dead_link(request, invite):
    return render(request, 'guests/public_link_dead.html',
                  _public_context(expired=bool(invite and invite.is_expired)),
                  status=410 if invite else 404)


def guest_portal(request, token):
    """Everything a guest may reach, and nothing else.

    The link is the only credential, so every query here is filtered by this
    invitation's own guest and tenant. A guest may read their own receipts and
    edit their own details — no invoices, no balances, no other guest, no part
    of the office.
    """
    invite = _live_invite(token)
    if invite is None or not invite.is_usable:
        return _dead_link(request, invite)
    invite.mark_opened()
    return render(request, 'guests/portal_home.html',
                  _public_context(invite, receipt_count=_receipt_count(invite)))


def _receipt_count(invite):
    from billing.models import Payment

    return (
        Payment.objects.filter(tenancy__guest=invite.guest, tenant=invite.tenant,
                               is_reversed=False).count()
        + DepositTransaction.objects.filter(tenancy__guest=invite.guest,
                                            tenant=invite.tenant).count()
    )


def guest_portal_profile(request, token):
    """The one thing a guest may change: their own details."""
    invite = _live_invite(token)
    if invite is None or not invite.is_usable:
        return _dead_link(request, invite)

    guest = invite.guest
    form = GuestSelfProfileForm(request.POST or None, request.FILES or None, instance=guest)

    if request.method == 'POST' and form.is_valid():
        fields = list(GuestSelfProfileForm.Meta.fields)
        before = audit.snapshot(guest, fields)
        form.save()
        invite.completed_at = timezone.now()
        invite.save(update_fields=['completed_at', 'updated_at'])
        audit.log(
            AuditLog.ACTION_UPDATE, guest, tenant=invite.tenant, actor=None,
            summary=_('%(name)s completed their own profile from a shared link')
                    % {'name': guest.full_name},
            changes=audit.diff(before, audit.snapshot(guest, fields)),
        )
        AuditLog.objects.filter(
            tenant=invite.tenant, actor__isnull=True, object_id=str(guest.pk)
        ).order_by('-created_at').update(
            actor_label=f'{guest.full_name} ({_("self-service link")})')
        return render(request, 'guests/public_done.html', _public_context(invite))

    if request.method == 'GET':
        invite.mark_opened()
    return render(request, 'guests/public_profile.html',
                  _public_context(invite, form=form))


def guest_portal_receipts(request, token):
    """Read-only. Their receipts, and only theirs."""
    invite = _live_invite(token)
    if invite is None or not invite.is_usable:
        return _dead_link(request, invite)

    from billing.models import Payment

    rent = Payment.objects.filter(
        tenancy__guest=invite.guest, tenant=invite.tenant, is_reversed=False
    ).select_related('tenancy__building', 'tenancy__room', 'tenancy__bed')
    deposits = DepositTransaction.objects.filter(
        tenancy__guest=invite.guest, tenant=invite.tenant
    ).select_related('tenancy__building', 'tenancy__room')

    rows = [
        {'kind': 'rent', 'pk': p.pk, 'number': p.receipt_number, 'date': p.paid_on,
         'amount': p.amount, 'label': _('Rent'), 'money_in': True,
         'note': str(p.mode or '')}
        for p in rent
    ] + [
        {'kind': 'deposit', 'pk': d.pk, 'number': d.receipt_number, 'date': d.happened_on,
         'amount': d.amount, 'label': d.get_kind_display(), 'money_in': d.is_money_in,
         'note': d.reason}
        for d in deposits
    ]
    rows.sort(key=lambda row: (row['date'], row['number']), reverse=True)
    return render(request, 'guests/portal_receipts.html',
                  _public_context(invite, rows=paginate(request, rows, 20)))


def guest_portal_receipt(request, token, kind, pk):
    """One receipt to read, only if it belongs to the guest holding this link."""
    invite = _live_invite(token)
    if invite is None or not invite.is_usable:
        return _dead_link(request, invite)

    from billing.models import Payment
    from billing.views import receipt_view_context

    if kind == 'rent':
        record = Payment.objects.filter(
            pk=pk, tenant=invite.tenant, tenancy__guest=invite.guest, is_reversed=False
        ).select_related('tenancy__guest', 'tenancy__building', 'tenancy__room',
                         'tenancy__bed', 'invoice', 'tenant').first()
        if record is None:
            raise Http404
        context = receipt_view_context(record)
        template = 'billing/public_receipt.html'
    else:
        record = DepositTransaction.objects.filter(
            pk=pk, tenant=invite.tenant, tenancy__guest=invite.guest
        ).select_related('tenancy__guest', 'tenancy__building', 'tenancy__room',
                         'tenancy__bed', 'tenant', 'recorded_by').first()
        if record is None:
            # Somebody else's receipt reads exactly like one that does not exist.
            raise Http404
        context = deposit_view_context(record)
        template = 'guests/public_deposit_receipt.html'

    context['back_to'] = reverse('guest_portal_receipts', args=[invite.token])
    response = render(request, template, context)
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def deposit_view_context(entry):
    return {
        'entry': entry,
        'tenancy': entry.tenancy,
        'guest': entry.tenancy.guest,
        'building': entry.tenancy.building,
        'org_name': entry.tenant.name,
        'org_currency': f'{entry.tenant.currency} ',
        'held': entry.tenancy.deposit_held(),
    }


def _deposit_entry(request, pk):
    return get_object_or_404(
        DepositTransaction.objects.select_related(
            'tenancy__guest', 'tenancy__building', 'tenancy__room', 'tenancy__bed',
            'tenant', 'recorded_by'),
        pk=pk, tenant=request.tenant,
    )


@require_module(MODULE_GUESTS)
def deposit_receipt(request, pk):
    from billing import receipts, sharing
    from billing.models import PrinterConfig

    entry = _deposit_entry(request, pk)
    printer = PrinterConfig.objects.filter(tenant=request.tenant, is_default=True).first() \
        or PrinterConfig.objects.filter(tenant=request.tenant).first()
    return render(request, 'guests/deposit_receipt.html', {
        'entry': entry,
        'printer': printer,
        'printers': PrinterConfig.objects.filter(tenant=request.tenant),
        'text': '\n'.join(receipts.deposit_receipt_lines(entry, printer)),
        'public_url': sharing.deposit_url(entry, request),
        'link_days': settings.ZYDWELL['RECEIPT_LINK_DAYS'],
    })


@require_module(MODULE_POS, 'view')
def deposit_receipt_print(request, pk):
    from billing import receipts
    from billing.models import PrinterConfig

    entry = _deposit_entry(request, pk)
    printer = PrinterConfig.objects.filter(
        pk=request.POST.get('printer'), tenant=request.tenant
    ).first() or PrinterConfig.objects.filter(tenant=request.tenant, is_default=True).first()
    payload = receipts.render_escpos(
        entry, printer, lines=receipts.deposit_receipt_lines(entry, printer))
    ok, note = receipts.send_to_printer(printer, payload)
    audit.log(AuditLog.ACTION_PRINT, entry,
              summary=_('Deposit receipt %(no)s sent to the printer: %(note)s') % {
                  'no': entry.receipt_number, 'note': note})
    (messages.success if ok else messages.error)(request, note)
    return redirect('guests:deposit_receipt', pk=entry.pk)


@require_module(MODULE_GUESTS)
def deposit_receipt_whatsapp(request, pk):
    from billing import sharing
    from messaging import services as messaging

    entry = _deposit_entry(request, pk)
    guest = entry.tenancy.guest
    url = sharing.deposit_url(entry, request)
    body = _(
        '%(title)s — %(currency)s %(amount)s for %(guest)s. Receipt %(no)s: %(link)s'
    ) % {'title': entry.receipt_title(), 'currency': request.tenant.currency,
         'amount': f'{entry.amount:,.2f}', 'guest': guest.full_name,
         'no': entry.receipt_number, 'link': url}

    log, chat = messaging.hand_to_whatsapp(
        tenant=request.tenant, template_code='receipt_share',
        to_number=guest.messaging_number(),
        context=messaging.base_context(request.tenant, guest=guest,
                                       tenancy=entry.tenancy, extra={'link': url}),
        guest=guest, tenancy=entry.tenancy, user=request.user, body_override=body,
        prefer_web=messaging.prefers_whatsapp_web(request),
    )
    audit.log(AuditLog.ACTION_PRINT, entry,
              summary=_('Shared deposit receipt %(no)s on WhatsApp') % {
                  'no': entry.receipt_number})
    if not chat:
        messages.error(request, log.error or _('No WhatsApp number for this guest.'))
        return redirect('guests:deposit_receipt', pk=entry.pk)
    return redirect(chat)


def public_deposit_receipt(request, token):
    """A guest reading their own deposit receipt. No account, no download."""
    from billing import sharing

    entry, problem = sharing.deposit_from_token(token)
    if entry is None:
        return render(request, 'billing/receipt_link_dead.html',
                      {'expired': problem == 'expired', 'org_name': 'ZyDwell'},
                      status=410 if problem == 'expired' else 404)
    response = render(request, 'guests/public_deposit_receipt.html',
                      deposit_view_context(entry))
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@require_module(MODULE_GUESTS)
def guest_statement(request, pk):
    """A printable, downloadable statement of everything the guest owes and paid."""
    guest = get_object_or_404(Guest, pk=pk, tenant=request.tenant)
    from billing.models import Invoice, Payment
    from billing.services import aging_buckets

    buckets = aging_buckets(request.tenant)
    invoices = Invoice.objects.filter(tenancy__guest=guest).select_related('status', 'tenancy')
    payments = Payment.objects.filter(tenancy__guest=guest, is_reversed=False)
    audit.log(AuditLog.ACTION_EXPORT, guest,
              summary=_('Statement viewed for %(name)s') % {'name': guest.full_name})
    return render(request, 'guests/statement.html', {
        'guest': guest,
        'invoices': invoices,
        'payments': payments,
        'outstanding': guest.outstanding_balance(),
        'credit': guest.advance_credit(),
        'aging_rows': [(label, buckets[label]) for label in
                       ('current', '0-30', '31-60', '60+')],
        'generated_at': timezone.now(),
    })
