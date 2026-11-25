from django.conf import settings
from django.urls import reverse

from . import navigation
from .models import FeatureFlag, SystemNotice


def branding(request):
    tenant = getattr(request, 'tenant', None)
    user = getattr(request, 'user', None)
    brand = getattr(tenant, 'branding', None) if tenant else None
    flags = {}
    if tenant:
        flags = dict(FeatureFlag.objects.filter(tenant=tenant).values_list('module', 'enabled'))
    notices = []
    if user is not None and getattr(user, 'is_authenticated', False):
        notices = list(
            SystemNotice.objects.filter(is_active=True).filter(
                models_q(tenant)
            )[:3]
        )
    nav_items = navigation.build(request)
    assistant_url = reverse('assistant:chat')
    return {
        'tenant': tenant,
        'brand': brand,
        'brand_primary': getattr(brand, 'primary_color', '#0f3f52'),
        'brand_accent': getattr(brand, 'accent_color', '#e0a03c'),
        'brand_name': getattr(brand, 'display_name', '') or (tenant.name if tenant else 'ZyDwell'),
        'feature_flags': flags,
        'system_notices': notices,
        'currency': tenant.currency if tenant else settings.ZYDWELL['DEFAULT_CURRENCY'],
        'impersonating': bool(request.session.get('impersonator_id')) if hasattr(request, 'session') else False,
        'nav_items': nav_items,
        # The floating chat button: for whoever may open the assistant, on
        # every page but the assistant's own.
        'assistant_launcher': request.path != assistant_url and any(
            item['url'] == assistant_url for item in nav_items),
        # A phone hands off to the WhatsApp app and abandons the tab, so open
        # chats in place there and only use a new tab on a desktop.
        'chat_new_tab': _wants_new_tab(request),
    }


def models_q(tenant):
    from django.db.models import Q

    if tenant is None:
        return Q(tenant__isnull=True)
    return Q(tenant__isnull=True) | Q(tenant=tenant)


def _wants_new_tab(request):
    from messaging.services import prefers_whatsapp_web

    return prefers_whatsapp_web(request)
