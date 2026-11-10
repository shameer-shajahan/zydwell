from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import resolve, reverse
from django.utils import translation
from django.utils.translation import gettext as _

from .current import set_current_request

#: URL namespaces reachable without an active, paid-up tenant.
OPEN_NAMESPACES = {'accounts', 'admin', 'platform'}


class CurrentRequestMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_current_request(request)
        try:
            return self.get_response(request)
        finally:
            set_current_request(None)


class TenantMiddleware:
    """Attach the active tenant to the request and honour the language choice."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        tenant = None
        if user is not None and user.is_authenticated:
            tenant = user.active_tenant(request.session)
            # The language picker writes a cookie and must win; the profile
            # setting is only the default for someone who has not chosen.
            chosen = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
            if not chosen and user.language:
                translation.activate(user.language)
                request.LANGUAGE_CODE = user.language
        request.tenant = tenant
        return self.get_response(request)


class PasswordChangeRequiredMiddleware:
    """A flagged account can do nothing until it picks a new password.

    Without this the flag is only a nudge at sign-in, and anyone handed a reset
    password could keep using it indefinitely — including the person who set it.
    """

    ALLOWED = {'accounts:profile', 'accounts:logout', 'set_language'}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated or not user.must_change_password:
            return self.get_response(request)

        match = resolve(request.path_info)
        name = f'{match.namespace}:{match.url_name}' if match.namespace else match.url_name
        if name in self.ALLOWED or request.path_info.startswith(settings.STATIC_URL):
            return self.get_response(request)

        messages.warning(request, _(
            'Choose a new password before carrying on — the one you were given is '
            'only for signing in this once.'))
        return redirect('accounts:profile')


class SubscriptionLockMiddleware:
    """Block a client whose subscription lapsed past its grace period."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        tenant = getattr(request, 'tenant', None)
        if user is None or not user.is_authenticated or tenant is None or user.is_super_admin:
            return self.get_response(request)

        try:
            namespace = resolve(request.path_info).namespace
        except Exception:
            namespace = None
        if namespace in OPEN_NAMESPACES:
            return self.get_response(request)

        subscription = getattr(tenant, 'subscription', None)
        locked = tenant.status != tenant.STATUS_ACTIVE
        if not locked and subscription and subscription.auto_lock and subscription.is_past_grace:
            locked = True

        if locked:
            return render(request, 'core/account_locked.html', {
                'tenant': tenant,
                'subscription': subscription,
            }, status=403)

        if subscription and subscription.is_expired and not subscription.is_past_grace:
            messages.warning(request, _(
                'Subscription expired on %(date)s. Access continues until the grace period ends.'
            ) % {'date': subscription.expires_on})
        return self.get_response(request)
