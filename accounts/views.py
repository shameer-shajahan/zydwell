"""Sign in, profile and the admin's staff / access-control screens."""
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import audit
from core.audit import client_ip
from core.navigation import landing_url
from core.models import AuditLog
from core.permissions import admin_required
from core.utils import paginate

from .forms import (
    AssignmentForm, LoginForm, PasswordChangeSimpleForm, PermissionForm, ProfileForm,
    StaffForm,
)
from .models import LoginHistory, StaffAssignment, User


def login_view(request):
    if request.user.is_authenticated:
        return redirect(landing_url(request.user, request.user.active_tenant(request.session)))
    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST':
        if form.is_valid():
            user = form.get_user()
            blocked = _ip_blocked(user, request)
            if blocked:
                messages.error(request, blocked)
                _record_login(request, user=user, ok=False)
            else:
                login(request, user)
                _record_login(request, user=user, ok=True)
                audit.log(AuditLog.ACTION_LOGIN, user, tenant=user.tenant,
                          summary=_('Signed in'), request=request)
                if user.must_change_password:
                    messages.warning(request, _('Please set a new password.'))
                    return redirect('accounts:profile')
                return redirect(landing_url(user, user.active_tenant(request.session)))
        else:
            _record_login(request, username=request.POST.get('username', ''), ok=False)
    return render(request, 'accounts/login.html', {'form': form})


def logout_view(request):
    if request.user.is_authenticated:
        audit.log(AuditLog.ACTION_LOGOUT, request.user, tenant=request.user.tenant,
                  summary=_('Signed out'), request=request)
    logout(request)
    return redirect('accounts:login')


def profile_view(request):
    if not request.user.is_authenticated:
        return redirect('accounts:login')
    profile_form = ProfileForm(instance=request.user)
    password_form = PasswordChangeSimpleForm(user=request.user)

    if request.method == 'POST' and request.POST.get('action') == 'profile':
        profile_form = ProfileForm(request.POST, request.FILES, instance=request.user)
        if profile_form.is_valid():
            profile_form.save()
            messages.success(request, _('Profile updated.'))
            return redirect('accounts:profile')
    elif request.method == 'POST' and request.POST.get('action') == 'password':
        password_form = PasswordChangeSimpleForm(request.POST, user=request.user)
        if password_form.is_valid():
            request.user.set_password(password_form.cleaned_data['new_password'])
            request.user.must_change_password = False
            request.user.save()
            update_session_auth_hash(request, request.user)
            audit.log(AuditLog.ACTION_UPDATE, request.user, tenant=request.user.tenant,
                      summary=_('Changed own password'))
            messages.success(request, _('Password changed.'))
            return redirect('accounts:profile')

    return render(request, 'accounts/profile.html', {
        'profile_form': profile_form,
        'password_form': password_form,
        'logins': request.user.login_history.all()[:15],
    })


@admin_required
def staff_list(request):
    rows = User.objects.filter(tenant=request.tenant, role=User.ROLE_STAFF).prefetch_related(
        'module_permissions', 'assignments__building', 'assignments__floor'
    )
    query = request.GET.get('q', '').strip()
    if query:
        rows = rows.filter(username__icontains=query) | rows.filter(first_name__icontains=query)
    active_seats = rows.filter(is_active=True).count()
    return render(request, 'accounts/staff_list.html', {
        'page': paginate(request, rows),
        'query': query,
        'active_seats': active_seats,
        'seat_limit': request.tenant.max_staff_seats,
    })


@admin_required
def staff_form(request, pk=None):
    staff = get_object_or_404(User, pk=pk, tenant=request.tenant, role=User.ROLE_STAFF) if pk else None
    before = audit.snapshot(staff) if staff else {}
    form = StaffForm(request.POST or None, request.FILES or None, instance=staff,
                     tenant=request.tenant)
    if request.method == 'POST' and form.is_valid():
        obj = form.save()
        audit.log(AuditLog.ACTION_UPDATE if staff else AuditLog.ACTION_CREATE, obj,
                  tenant=request.tenant,
                  summary=_('Staff account %(name)s saved') % {'name': obj.display_name()},
                  changes=audit.diff(before, audit.snapshot(obj)) if staff else {})
        messages.success(request, _('Staff account saved.'))
        return redirect('accounts:staff_permissions', pk=obj.pk)
    return render(request, 'accounts/staff_form.html', {'form': form, 'staff': staff})


@admin_required
def staff_permissions(request, pk):
    staff = get_object_or_404(User, pk=pk, tenant=request.tenant, role=User.ROLE_STAFF)
    form = PermissionForm(request.POST or None, staff=staff)
    assignment_form = AssignmentForm(tenant=request.tenant)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'permissions' and form.is_valid():
            form.save()
            audit.log(AuditLog.ACTION_UPDATE, staff, tenant=request.tenant,
                      summary=_('Updated module permissions for %(name)s') % {
                          'name': staff.display_name()},
                      changes={k: v for k, v in form.cleaned_data.items() if v})
            messages.success(request, _('Permissions saved.'))
            return redirect('accounts:staff_permissions', pk=staff.pk)
        if request.POST.get('action') == 'assign':
            assignment_form = AssignmentForm(request.POST, tenant=request.tenant)
            if assignment_form.is_valid():
                assignment = assignment_form.save(commit=False)
                assignment.user = staff
                if StaffAssignment.objects.filter(
                    user=staff, building=assignment.building, floor=assignment.floor
                ).exists():
                    messages.info(request, _('That assignment already exists.'))
                else:
                    assignment.save()
                    audit.log(AuditLog.ACTION_CREATE, assignment, tenant=request.tenant,
                              summary=_('Assigned %(name)s to %(where)s') % {
                                  'name': staff.display_name(), 'where': assignment})
                    messages.success(request, _('Assignment added.'))
                return redirect('accounts:staff_permissions', pk=staff.pk)

    return render(request, 'accounts/staff_permissions.html', {
        'staff': staff,
        'form': form,
        'assignment_form': assignment_form,
        'assignments': staff.assignments.select_related('building', 'floor'),
    })


@admin_required
def assignment_delete(request, pk):
    assignment = get_object_or_404(
        StaffAssignment, pk=pk, user__tenant=request.tenant
    )
    staff_pk = assignment.user_id
    if request.method == 'POST':
        audit.log(AuditLog.ACTION_DELETE, assignment, tenant=request.tenant,
                  summary=_('Removed assignment %(where)s') % {'where': assignment})
        assignment.delete()
        messages.success(request, _('Assignment removed.'))
    return redirect('accounts:staff_permissions', pk=staff_pk)


@admin_required
def staff_toggle(request, pk):
    staff = get_object_or_404(User, pk=pk, tenant=request.tenant, role=User.ROLE_STAFF)
    if request.method == 'POST':
        staff.is_active = not staff.is_active
        staff.save(update_fields=['is_active'])
        audit.log(AuditLog.ACTION_UPDATE, staff, tenant=request.tenant,
                  summary=_('%(state)s staff account %(name)s') % {
                      'state': _('Activated') if staff.is_active else _('Deactivated'),
                      'name': staff.display_name()})
        messages.success(request, _('Account updated.'))
    return redirect('accounts:staff_list')


@admin_required
def login_history(request):
    rows = LoginHistory.objects.filter(tenant=request.tenant).select_related('user')
    return render(request, 'accounts/login_history.html', {'page': paginate(request, rows, 40)})


def _record_login(request, user=None, username='', ok=True):
    LoginHistory.objects.create(
        user=user if ok else user,
        username_tried=username or (user.username if user else ''),
        tenant=user.tenant if user else None,
        successful=ok,
        ip_address=client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:255],
    )


def _ip_blocked(user, request):
    """Super admin accounts may be pinned to an IP allow-list."""
    if not user.is_super_admin or not user.ip_allow_list.strip():
        return None
    allowed = {ip.strip() for ip in user.ip_allow_list.split(',') if ip.strip()}
    if client_ip(request) not in allowed:
        return _('This account may only sign in from an approved IP address.')
    return None
