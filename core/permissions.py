"""Decorators that enforce role, per-module permission and feature flags."""
from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.utils.translation import gettext as _

from .models import FeatureFlag


def _needs_tenant(request):
    if request.tenant is None:
        if request.user.is_super_admin:
            messages.info(request, _('Pick a client account to work inside.'))
            return redirect('platform:tenant_list')
        messages.error(request, _('Your account is not linked to a client.'))
        return redirect('accounts:logout')
    return None


def require_module(module, action='view'):
    """Gate a view on the active tenant, the feature flag and the user's grant."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('accounts:login')
            redirect_response = _needs_tenant(request)
            if redirect_response is not None:
                return redirect_response
            flag = FeatureFlag.objects.filter(tenant=request.tenant, module=module).first()
            if flag is not None and not flag.enabled:
                raise PermissionDenied(_('This module is switched off for your account.'))
            if not request.user.can(module, action):
                raise PermissionDenied(
                    _('You do not have permission to %(action)s in this module.') % {'action': action}
                )
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def signed_in_with_tenant(view):
    """For a screen everyone may open — it decides for itself what to show."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('accounts:login')
        redirect_response = _needs_tenant(request)
        if redirect_response is not None:
            return redirect_response
        return view(request, *args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('accounts:login')
        if not (request.user.is_admin or request.user.is_super_admin):
            raise PermissionDenied(_('Only the building owner can do this.'))
        redirect_response = _needs_tenant(request)
        if redirect_response is not None:
            return redirect_response
        return view(request, *args, **kwargs)

    return wrapper


def super_admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('accounts:login')
        if not request.user.is_super_admin:
            raise PermissionDenied(_('Platform console is restricted.'))
        return view(request, *args, **kwargs)

    return wrapper
