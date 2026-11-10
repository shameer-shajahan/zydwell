"""WhatsApp templates, manual sends, broadcasts and the delivery log."""
from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _

from core import audit
from core.constants import MODULE_MESSAGING
from core.filters import LocationFilter
from core.models import AuditLog, whatsapp_config
from core.permissions import admin_required, require_module
from core.utils import date_range_from_request, paginate, safe_next
from guests.models import Guest, Tenancy

from . import services
from .forms import BroadcastForm, ManualSendForm, ReminderRuleForm, TemplateForm
from .models import MessageLog, MessageTemplate, ReminderRule


def _scoped_messages(request):
    """Staff see the messages for the floors they look after, like every other list."""
    rows = MessageLog.objects.filter(tenant=request.tenant).select_related(
        'guest', 'sent_by', 'invoice', 'tenancy__building', 'tenancy__floor',
        'tenancy__apartment', 'tenancy__room', 'tenancy__bed')
    return request.user.scope_queryset(
        rows, building_field='tenancy__building', floor_field='tenancy__floor',
        apartment_field='tenancy__apartment')


def _sender_summary(rows):
    """How many each person sent, and how many actually got away."""
    landed = [MessageLog.STATUS_OPENED, MessageLog.STATUS_SENT,
              MessageLog.STATUS_DELIVERED, MessageLog.STATUS_READ]
    counts = rows.values('sent_by_id').annotate(
        total=Count('id'),
        sent=Count('id', filter=Q(status__in=landed)),
        waiting=Count('id', filter=Q(status=MessageLog.STATUS_QUEUED)),
        failed=Count('id', filter=Q(status=MessageLog.STATUS_FAILED)),
    ).order_by('-total')

    from accounts.models import User

    people = {user.pk: user for user in User.objects.filter(
        pk__in=[row['sent_by_id'] for row in counts if row['sent_by_id']])}
    summary = []
    for row in counts:
        person = people.get(row['sent_by_id'])
        summary.append({
            'who': person.display_name() if person else _('Automatic (nightly job)'),
            'role': person.get_role_display() if person else _('System'),
            'is_person': person is not None,
            **{key: row[key] for key in ('total', 'sent', 'waiting', 'failed')},
        })
    return summary


@require_module(MODULE_MESSAGING)
def log(request):
    services.purge_old_messages_daily(request.tenant)
    location = LocationFilter(request)
    rows = location.apply(_scoped_messages(request), prefix='tenancy__')
    start, end = date_range_from_request(request, default_days=30)
    rows = rows.filter(created_at__date__range=(start, end))

    status = request.GET.get('status', '')
    if status:
        rows = rows.filter(status=status)
    sender = request.GET.get('sender', '')
    if sender == 'system':
        rows = rows.filter(sent_by__isnull=True)
    elif sender:
        rows = rows.filter(sent_by_id=sender)
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(Q(to_number__icontains=query) | Q(body__icontains=query)
                           | Q(guest__full_name__icontains=query))

    from accounts.models import User

    return render(request, 'messaging/log.html', {
        'page': paginate(request, rows, 30),
        'status': status, 'query': query, 'sender': sender,
        'start': start, 'end': end,
        'statuses': MessageLog.STATUS_CHOICES,
        'senders': User.objects.filter(tenant=request.tenant, is_active=True),
        'by_sender': _sender_summary(rows),
        'can_send': request.user.can(MODULE_MESSAGING, 'create'),
        'can_delete': request.user.can(MODULE_MESSAGING, 'delete'),
        'keep_days': settings.ZYDWELL['MESSAGE_LOG_DAYS'],
        'location': location,
    })


@require_module(MODULE_MESSAGING, 'delete')
def log_delete(request, pk):
    entry = get_object_or_404(_scoped_messages(request), pk=pk)
    if request.method == 'POST':
        audit.log(AuditLog.ACTION_DELETE, entry,
                  summary=_('Deleted a WhatsApp log entry to %(number)s') % {
                      'number': entry.to_number or '—'})
        entry.delete()
        messages.success(request, _('Message deleted from the log.'))
    back = request.POST.get('next', '')
    if url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()},
                                       require_https=request.is_secure()):
        return redirect(back)
    return redirect('messaging:log')


@require_module(MODULE_MESSAGING, 'create')
def send_manual(request):
    initial = {}
    for key in ('guest', 'template'):
        if request.GET.get(key):
            initial[key] = request.GET[key]
    if request.GET.get('template') and not str(request.GET['template']).isdigit():
        template = MessageTemplate.objects.filter(
            tenant=request.tenant, code=request.GET['template']
        ).first()
        initial['template'] = template.pk if template else None

    form = ManualSendForm(request.POST or None, tenant=request.tenant, initial=initial)
    if request.method == 'POST' and form.is_valid():
        guest = form.cleaned_data['guest']
        template = form.cleaned_data['template']
        tenancy = guest.current_tenancy()
        invoice = None
        if request.GET.get('invoice'):
            from billing.models import Invoice

            invoice = Invoice.objects.filter(
                pk=request.GET['invoice'], tenant=request.tenant
            ).first()
        payment = None
        if request.GET.get('payment'):
            from billing.models import Payment

            payment = Payment.objects.filter(
                pk=request.GET['payment'], tenant=request.tenant
            ).first()

        context = services.base_context(
            request.tenant, guest=guest, tenancy=tenancy, invoice=invoice, payment=payment,
            extra={'reason': form.cleaned_data['reason'],
                   'message': form.cleaned_data['body']},
        )
        code = template.code if template else 'building_notice'
        if request.POST.get('open_in_whatsapp'):
            entry, url = services.hand_to_whatsapp(
                tenant=request.tenant, template_code=code,
                to_number=guest.messaging_number(), context=context, guest=guest,
                tenancy=tenancy, invoice=invoice, user=request.user,
                body_override=form.cleaned_data['body'],
                prefer_web=services.prefers_whatsapp_web(request),
            )
            audit.log(AuditLog.ACTION_CREATE, entry,
                      summary=_('Opened a message to %(g)s in WhatsApp') % {
                          'g': guest.full_name})
            if url:
                return redirect(url)
            messages.error(request, entry.error or _('That message could not be prepared.'))
            return redirect('messaging:log')

        entry = services.send(
            tenant=request.tenant, template_code=code,
            to_number=guest.messaging_number(), context=context, guest=guest,
            tenancy=tenancy,
            invoice=invoice, user=request.user,
            body_override=form.cleaned_data['body'],
        )
        audit.log(AuditLog.ACTION_CREATE, entry,
                  summary=_('Message to %(g)s: %(status)s') % {
                      'g': guest.full_name, 'status': entry.get_status_display()})
        messages.success(request, _('Message %(status)s.') % {
            'status': entry.get_status_display().lower()})
        return redirect('messaging:log')
    return render(request, 'messaging/send.html', {'form': form})


@require_module(MODULE_MESSAGING, 'create')
def broadcast(request):
    form = BroadcastForm(request.POST or None, tenant=request.tenant)
    if request.method == 'POST' and form.is_valid():
        tenancies = Tenancy.objects.filter(
            tenant=request.tenant, status__code__in=Tenancy.LIVE_STATUS_CODES
        ).select_related('guest', 'building')
        if form.cleaned_data['building']:
            tenancies = tenancies.filter(building=form.cleaned_data['building'])
        sent = 0
        for tenancy in tenancies:
            services.send(
                tenant=request.tenant, template_code='building_notice',
                to_number=tenancy.guest.messaging_number(),
                context=services.base_context(
                    request.tenant, guest=tenancy.guest, tenancy=tenancy,
                    extra={'message': form.cleaned_data['message']},
                ),
                guest=tenancy.guest, tenancy=tenancy, user=request.user,
            )
            sent += 1
        audit.log(AuditLog.ACTION_CREATE, None, tenant=request.tenant,
                  model_name='MessageLog', object_label='broadcast',
                  summary=_('Broadcast sent to %(n)d guests') % {'n': sent})
        messages.success(request, _('Notice queued for %(n)d guests.') % {'n': sent})
        return redirect('messaging:log')
    return render(request, 'messaging/broadcast.html', {'form': form})


@require_module(MODULE_MESSAGING, 'create')
def quick_send(request):
    """One tap from anywhere in the app: log it, then open WhatsApp with it."""
    guest = get_object_or_404(Guest, pk=request.GET.get('guest'), tenant=request.tenant)
    code = request.GET.get('template', 'building_notice')
    tenancy = guest.current_tenancy()

    from billing.models import Invoice, Payment

    invoice = payment = None
    if request.GET.get('invoice'):
        invoice = Invoice.objects.filter(pk=request.GET['invoice'],
                                         tenant=request.tenant).first()
    if request.GET.get('payment'):
        payment = Payment.objects.filter(pk=request.GET['payment'],
                                         tenant=request.tenant).first()
        if payment and not invoice:
            invoice = payment.invoice

    # A rent reminder sent from the guest's page carries no invoice, but the
    # figures it needs are sitting right there — take the oldest unpaid one
    # rather than sending "rent of AED  is due on ."
    if invoice is None and 'rent' in code or code == 'final_notice':
        invoice = (
            Invoice.objects.filter(tenant=request.tenant, tenancy__guest=guest)
            .exclude(status__counts_as_settled=True)
            .order_by('due_date').first()
        )

    context = services.base_context(request.tenant, guest=guest, tenancy=tenancy,
                                    invoice=invoice, payment=payment)
    entry, url = services.hand_to_whatsapp(
        tenant=request.tenant, template_code=code, to_number=guest.messaging_number(),
        context=context, guest=guest, tenancy=tenancy, invoice=invoice, user=request.user,
        prefer_web=services.prefers_whatsapp_web(request),
    )
    audit.log(AuditLog.ACTION_CREATE, entry,
              summary=_('Opened a %(code)s message to %(name)s in WhatsApp') % {
                  'code': code, 'name': guest.full_name})
    if not url:
        messages.error(request, entry.error or _('That message could not be prepared.'))
        return redirect(safe_next(request) or 'messaging:log')
    return redirect(url)


@require_module(MODULE_MESSAGING, 'create')
def open_log(request, pk):
    """Open a message that was already prepared — the waiting list."""
    entry = get_object_or_404(MessageLog, pk=pk, tenant=request.tenant)
    url = services.reopen(entry, user=request.user,
                          prefer_web=services.prefers_whatsapp_web(request))
    if not url:
        messages.error(request, _('There is no usable number on that message.'))
        return redirect('messaging:waiting')
    audit.log(AuditLog.ACTION_CREATE, entry,
              summary=_('Opened a waiting message to %(number)s in WhatsApp') % {
                  'number': entry.to_number})
    return redirect(url)


@require_module(MODULE_MESSAGING)
def waiting(request):
    """Messages the system prepared but nobody has sent yet.

    With the Business API switched off, every automatic reminder lands here
    instead of vanishing. Staff work down the list, one tap each.
    """
    location = LocationFilter(request)
    rows = location.apply(_scoped_messages(request), prefix='tenancy__').filter(
        status=MessageLog.STATUS_QUEUED)
    mine = request.GET.get('mine') == '1'
    if mine:
        rows = rows.filter(tenancy__isnull=False)

    return render(request, 'messaging/waiting.html', {
        'page': paginate(request, rows, 30),
        'location': location,
        'total': rows.count(),
        'can_send': request.user.can(MODULE_MESSAGING, 'create'),
        'api_on': whatsapp_config(request.tenant)['ENABLED'],
        'sent_today': _scoped_messages(request).filter(
            sent_by=request.user,
            sent_at__date=timezone.localdate()).count(),
    })


@admin_required
def template_list(request):
    return render(request, 'messaging/templates.html', {
        'templates': paginate(request, MessageTemplate.objects.filter(tenant=request.tenant), 25),
        'rules': paginate(request, ReminderRule.objects.filter(
            tenant=request.tenant).select_related('template'), 25, param='rule_page'),
        'rule_form': ReminderRuleForm(tenant=request.tenant),
    })


@admin_required
def template_edit(request, pk):
    template = get_object_or_404(MessageTemplate, pk=pk, tenant=request.tenant)
    before = audit.snapshot(template, ['body', 'is_approved', 'is_active'])
    form = TemplateForm(request.POST or None, instance=template)
    if request.method == 'POST' and form.is_valid():
        obj = form.save()
        audit.log(AuditLog.ACTION_UPDATE, obj,
                  summary=_('Template %(n)s updated') % {'n': obj.name},
                  changes=audit.diff(before, audit.snapshot(
                      obj, ['body', 'is_approved', 'is_active'])))
        messages.success(request, _('Template saved.'))
        return redirect('messaging:template_list')
    return render(request, 'messaging/template_form.html', {'form': form, 'template': template})


@admin_required
def rule_save(request):
    form = ReminderRuleForm(request.POST or None, tenant=request.tenant)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        existing = ReminderRule.objects.filter(
            tenant=request.tenant, offset_days=obj.offset_days
        ).first()
        if existing:
            existing.template = obj.template
            existing.is_active = obj.is_active
            existing.save()
        else:
            obj.save()
        messages.success(request, _('Reminder schedule updated.'))
    return redirect('messaging:template_list')


@admin_required
def rule_delete(request, pk):
    rule = get_object_or_404(ReminderRule, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        rule.delete()
        messages.success(request, _('Reminder removed.'))
    return redirect('messaging:template_list')
