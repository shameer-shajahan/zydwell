"""Platform console — the super admin tier that sits above every client admin."""
from django.contrib import messages
from django.contrib.auth import login
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from . import audit, seed
from .forms import (
    AdminPasswordForm, BrandingForm, GlobalStatusForm, IntegrationSettingsForm,
    SubscriptionForm, SystemNoticeForm, TenantForm,
)
from .models import (
    AuditLog, Branding, FeatureFlag, IntegrationSettings, Status, Subscription,
    SystemNotice, Tenant,
)
from .permissions import super_admin_required
from .utils import paginate


@super_admin_required
def tenant_list(request):
    rows = Tenant.objects.order_by('name').select_related('subscription').annotate(
        user_count=Count('users', distinct=True),
    )
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(Q(name__icontains=query) | Q(contact_person__icontains=query))
    page = paginate(request, rows, 24)
    cards = []
    for tenant in page:
        cards.append({'obj': tenant, 'limits': tenant.limit_report(),
                      'subscription': getattr(tenant, 'subscription', None)})
    return render(request, 'core/tenant_list.html', {
        'cards': cards, 'query': query, 'page': page,
        'active_tenant_id': request.session.get('active_tenant_id'),
    })


@super_admin_required
def tenant_form(request, pk=None):
    tenant = get_object_or_404(Tenant, pk=pk) if pk else None
    before = audit.snapshot(tenant) if tenant else {}
    form = TenantForm(request.POST or None, instance=tenant)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.slug = form.cleaned_data['slug']
        obj.save()
        created_admin = None
        if tenant is None:
            seed.seed_tenant(obj)
            from accounts.models import User

            created_admin = User.objects.create_user(
                username=form.cleaned_data['admin_username'],
                email=form.cleaned_data['admin_email'],
                password=form.cleaned_data['admin_password'],
                role=User.ROLE_ADMIN,
                tenant=obj,
                first_name=obj.contact_person or obj.name,
                must_change_password=True,
            )
        audit.log(AuditLog.ACTION_UPDATE if tenant else AuditLog.ACTION_CREATE, obj, tenant=obj,
                  summary=_('Client %(n)s saved') % {'n': obj.name},
                  changes=audit.diff(before, audit.snapshot(obj)) if tenant else {})
        if created_admin:
            messages.success(request, _(
                'Client created with owner account "%(u)s". They must change the password on first sign-in.'
            ) % {'u': created_admin.username})
        else:
            messages.success(request, _('Client saved.'))
        return redirect('platform:tenant_detail', pk=obj.pk)
    return render(request, 'core/tenant_form.html', {'form': form, 'tenant_obj': tenant})


@super_admin_required
def tenant_detail(request, pk):
    tenant = get_object_or_404(Tenant, pk=pk)
    subscription = getattr(tenant, 'subscription', None)
    branding, _created = Branding.objects.get_or_create(
        tenant=tenant, defaults={'display_name': tenant.name})

    sub_form = SubscriptionForm(instance=subscription)
    brand_form = BrandingForm(instance=branding)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'subscription':
            sub_form = SubscriptionForm(request.POST, instance=subscription)
            if sub_form.is_valid():
                obj = sub_form.save(commit=False)
                obj.tenant = tenant
                obj.save()
                audit.log(AuditLog.ACTION_UPDATE, obj, tenant=tenant,
                          summary=_('Subscription updated for %(n)s') % {'n': tenant.name})
                messages.success(request, _('Subscription saved.'))
                return redirect('platform:tenant_detail', pk=tenant.pk)
        elif action == 'branding':
            brand_form = BrandingForm(request.POST, request.FILES, instance=branding)
            if brand_form.is_valid():
                brand_form.save()
                audit.log(AuditLog.ACTION_UPDATE, branding, tenant=tenant,
                          summary=_('Branding updated for %(n)s') % {'n': tenant.name})
                messages.success(request, _('Branding saved.'))
                return redirect('platform:tenant_detail', pk=tenant.pk)
        elif action == 'flags':
            for module, _label in FeatureFlag.MODULES:
                FeatureFlag.objects.update_or_create(
                    tenant=tenant, module=module,
                    defaults={'enabled': request.POST.get(f'flag_{module}') == 'on'},
                )
            audit.log(AuditLog.ACTION_UPDATE, tenant, tenant=tenant,
                      summary=_('Feature flags updated for %(n)s') % {'n': tenant.name})
            messages.success(request, _('Modules updated.'))
            return redirect('platform:tenant_detail', pk=tenant.pk)
        elif action == 'reseed':
            seed.seed_tenant(tenant, with_subscription=False)
            audit.log(AuditLog.ACTION_UPDATE, tenant, tenant=tenant,
                      summary=_('Master data re-seeded for %(n)s') % {'n': tenant.name})
            messages.success(request, _('Missing master data has been added.'))
            return redirect('platform:tenant_detail', pk=tenant.pk)

    from accounts.models import User
    from billing.models import Payment
    from messaging.models import MessageLog

    return render(request, 'core/tenant_detail.html', {
        'tenant_obj': tenant,
        'subscription': subscription,
        'sub_form': sub_form,
        'brand_form': brand_form,
        'modules': _module_rows(tenant),
        'limits': tenant.limit_report(),
        'admins': User.objects.filter(tenant=tenant, role=User.ROLE_ADMIN),
        'staff_count': User.objects.filter(tenant=tenant, role=User.ROLE_STAFF).count(),
        'collected': Payment.objects.filter(tenant=tenant, is_reversed=False).aggregate(
            s=Sum('amount'))['s'] or 0,
        'message_failures': MessageLog.objects.filter(
            tenant=tenant, status=MessageLog.STATUS_FAILED).count(),
    })


def _module_rows(tenant):
    flags = {f.module: f.enabled for f in tenant.feature_flags.all()}
    return [{'key': key, 'label': label, 'enabled': flags.get(key, True)}
            for key, label in FeatureFlag.MODULES]


@super_admin_required
def tenant_integrations(request, pk):
    """API keys the platform owner holds on this client's behalf."""
    tenant = get_object_or_404(Tenant, pk=pk)
    row, _created = IntegrationSettings.objects.get_or_create(tenant=tenant)
    form = IntegrationSettingsForm(request.POST or None, instance=row)

    if request.method == 'POST' and form.is_valid():
        watched = ['whatsapp_enabled', 'whatsapp_phone_number_id', 'email_enabled',
                   'email_host', 'sms_enabled', 'sms_provider', 'payment_provider']
        before = audit.snapshot(row, watched)
        form.save()
        row.refresh_from_db()
        # Credentials themselves never reach the audit log — only that they moved.
        changed = [name for name in IntegrationSettings.SECRET_FIELDS
                   if (form.data.get(name) or '').strip()]
        audit.log(AuditLog.ACTION_UPDATE, row, tenant=tenant,
                  summary=_('Integration settings updated for %(n)s%(keys)s') % {
                      'n': tenant.name,
                      'keys': _(' (new %(fields)s)') % {'fields': ', '.join(changed)}
                              if changed else ''},
                  changes=audit.diff(before, audit.snapshot(row, watched)))
        messages.success(request, _('Integration settings saved.'))
        return redirect('platform:tenant_integrations', pk=tenant.pk)

    return render(request, 'core/tenant_integrations.html', {
        'tenant_obj': tenant,
        'form': form,
        'row': row,
    })


@super_admin_required
def tenant_status(request, pk):
    tenant = get_object_or_404(Tenant, pk=pk)
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in dict(Tenant.STATUS_CHOICES):
            before = tenant.status
            tenant.status = new_status
            tenant.save(update_fields=['status', 'updated_at'])
            audit.log(AuditLog.ACTION_UPDATE, tenant, tenant=tenant,
                      summary=_('Client %(n)s set to %(s)s') % {
                          'n': tenant.name, 's': tenant.get_status_display()},
                      changes={'status': [before, new_status]})
            messages.success(request, _('Client status updated.'))
    return redirect('platform:tenant_detail', pk=tenant.pk)


@super_admin_required
def reset_admin_password(request, pk):
    """Give a building owner a new password. No old password needed."""
    from accounts.models import User

    target = get_object_or_404(User, pk=pk, role=User.ROLE_ADMIN)
    form = AdminPasswordForm(request.POST or None, target=target)
    issued = None

    if request.method == 'POST' and form.is_valid():
        password, generated = form.resolve()
        target.set_password(password)
        target.must_change_password = True
        target.save(update_fields=['password', 'must_change_password'])
        # The password itself is never written down anywhere.
        audit.log(AuditLog.ACTION_UPDATE, target, tenant=target.tenant,
                  summary=_('Password reset for %(who)s by the platform owner') % {
                      'who': target.display_name()},
                  changes={'password': ['(unchanged)', '(reset)'],
                           'must_change_password': [False, True]})
        issued = password if generated else None
        if generated:
            form = AdminPasswordForm(target=target)
        else:
            messages.success(request, _(
                'Password set. %(who)s must choose a new one when they next sign in.'
            ) % {'who': target.display_name()})
            return redirect('platform:tenant_detail', pk=target.tenant_id)

    return render(request, 'core/admin_password.html', {
        'target': target,
        'tenant_obj': target.tenant,
        'form': form,
        'issued': issued,
    })


@super_admin_required
def switch_tenant(request, pk):
    """Work inside a client account without becoming one of its users."""
    tenant = get_object_or_404(Tenant, pk=pk)
    request.session['active_tenant_id'] = tenant.pk
    audit.log(AuditLog.ACTION_LOGIN, tenant, tenant=tenant,
              summary=_('Super admin opened %(n)s') % {'n': tenant.name})
    messages.info(request, _('Now working inside %(n)s.') % {'n': tenant.name})
    return redirect('reports:dashboard')


@super_admin_required
def impersonate(request, pk):
    """Sign in as a client admin for support. The action is recorded."""
    from accounts.models import User

    target = get_object_or_404(User, pk=pk, role=User.ROLE_ADMIN)
    if request.method != 'POST':
        return redirect('platform:tenant_detail', pk=target.tenant_id)
    audit.log(AuditLog.ACTION_IMPERSONATE, target, tenant=target.tenant,
              summary=_('%(who)s signed in as %(target)s for support') % {
                  'who': request.user.display_name(), 'target': target.display_name()})
    impersonator_id = request.user.pk
    login(request, target, backend='django.contrib.auth.backends.ModelBackend')
    request.session['impersonator_id'] = impersonator_id
    messages.warning(request, _('You are now signed in as %(n)s. Everything you do is logged.')
                     % {'n': target.display_name()})
    return redirect('reports:dashboard')


def stop_impersonation(request):
    from accounts.models import User

    impersonator_id = request.session.pop('impersonator_id', None)
    if not impersonator_id:
        return redirect('reports:dashboard')
    owner = User.objects.filter(pk=impersonator_id).first()
    if owner is None:
        return redirect('accounts:logout')
    audit.log(AuditLog.ACTION_IMPERSONATE, request.user, tenant=request.user.tenant,
              summary=_('Support session ended'), actor=owner)
    login(request, owner, backend='django.contrib.auth.backends.ModelBackend')
    messages.info(request, _('Back on the platform console.'))
    return redirect('platform:tenant_list')


@super_admin_required
def global_audit(request):
    rows = AuditLog.objects.all().select_related('actor', 'tenant')
    tenant_id = request.GET.get('tenant', '')
    if tenant_id:
        rows = rows.filter(tenant_id=tenant_id)
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(Q(summary__icontains=query) | Q(actor_label__icontains=query))
    return render(request, 'core/global_audit.html', {
        'page': paginate(request, rows, 50),
        'tenants': Tenant.objects.all(),
        'tenant_id': tenant_id, 'query': query,
    })


@super_admin_required
def system_health(request):
    from billing.models import PrinterConfig
    from messaging.models import MessageLog

    failures = MessageLog.objects.filter(status=MessageLog.STATUS_FAILED).select_related(
        'tenant', 'guest')[:30]
    printers = PrinterConfig.objects.select_related('tenant')
    storage = (
        Tenant.objects.annotate(
            guests=Count('guest_set', distinct=True),
            docs=Count('guestdocument_set', distinct=True),
        )
    )
    return render(request, 'core/system_health.html', {
        'failures': failures,
        'printers': printers,
        'storage': storage,
        'notices': SystemNotice.objects.all()[:10],
        'notice_form': SystemNoticeForm(),
        'expiring_subs': Subscription.objects.filter(
            expires_on__lte=timezone.localdate() + timezone.timedelta(days=30)
        ).select_related('tenant'),
    })


@super_admin_required
def notice_save(request):
    form = SystemNoticeForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        notice = form.save()
        audit.log(AuditLog.ACTION_CREATE, notice, tenant=notice.tenant,
                  summary=_('Notice published: %(t)s') % {'t': notice.title})
        messages.success(request, _('Notice published.'))
    return redirect('platform:system_health')


@super_admin_required
def notice_toggle(request, pk):
    notice = get_object_or_404(SystemNotice, pk=pk)
    if request.method == 'POST':
        notice.is_active = not notice.is_active
        notice.save(update_fields=['is_active'])
    return redirect('platform:system_health')


@super_admin_required
def global_statuses(request):
    """The factory default status set pushed into every new client."""
    form = GlobalStatusForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = None
        if Status.objects.filter(tenant=None, kind=obj.kind, code=obj.code).exists():
            messages.error(request, _('That code already exists in this kind.'))
        else:
            obj.save()
            messages.success(request, _('Default status added.'))
            return redirect('platform:global_statuses')
    return render(request, 'core/global_statuses.html', {
        'rows': paginate(request, Status.objects.filter(tenant=None), 25),
        'form': form,
    })
