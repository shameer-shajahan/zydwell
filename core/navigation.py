"""The sidebar, built from the user's role, grants and feature flags."""
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .constants import (
    MODULE_ACCOUNTING, MODULE_BILLING, MODULE_GUESTS, MODULE_MESSAGING,
    MODULE_POS, MODULE_PROPERTIES, MODULE_REPORTS, MODULE_SETTINGS, MODULE_STAFF,
)

# (module, url name, label, icon glyph)
NAV_ITEMS = [
    # No module: everyone has a dashboard, it just shows them different things.
    (None, 'reports:dashboard', _('Dashboard'), 'M3 12h4l3 8 4-16 3 8h4'),
    (MODULE_PROPERTIES, 'properties:building_list', _('Buildings'), 'M3 21h18M5 21V7l7-4 7 4v14'),
    (MODULE_PROPERTIES, 'properties:bed_board', _('Bed board'), 'M3 18v-6a2 2 0 012-2h14a2 2 0 012 2v6M3 18h18M7 10V7h10v3'),
    (MODULE_GUESTS, 'guests:guest_list', _('Guests'), 'M16 21v-2a4 4 0 00-8 0v2M12 11a4 4 0 100-8 4 4 0 000 8z'),
    (MODULE_GUESTS, 'guests:tenancy_list', _('Tenancies'), 'M9 12h6m-6 4h6M7 3h10a2 2 0 012 2v14a2 2 0 01-2 2H7a2 2 0 01-2-2V5a2 2 0 012-2z'),
    (MODULE_BILLING, 'billing:invoice_list', _('Invoices'), 'M9 14l2 2 4-4M7 3h10a2 2 0 012 2v14l-3-2-2 2-2-2-2 2-2-2-3 2V5a2 2 0 012-2z'),
    (MODULE_BILLING, 'billing:payment_list', _('Payments'), 'M3 10h18M3 7a2 2 0 012-2h14a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2V7z'),
    (MODULE_BILLING, 'billing:overdue', _('Overdue & aging'), 'M12 8v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z'),
    (MODULE_ACCOUNTING, 'accounting:ledger', _('Income & expense'), 'M4 19V5m0 14h16M8 15V9m4 6V6m4 9v-4'),
    (MODULE_MESSAGING, 'messaging:waiting', _('WhatsApp'), 'M21 11.5a8.5 8.5 0 01-12.5 7.5L3 21l2-5.5A8.5 8.5 0 1121 11.5z'),
    (MODULE_REPORTS, 'reports:report_index', _('Reports'), 'M9 17V9m4 8V5m4 12v-5M4 21h16a1 1 0 001-1V4a1 1 0 00-1-1H4a1 1 0 00-1 1v16a1 1 0 001 1z'),
    (MODULE_POS, 'billing:printer_list', _('POS printers'), 'M6 9V3h12v6M6 18H4a2 2 0 01-2-2v-4a2 2 0 012-2h16a2 2 0 012 2v4a2 2 0 01-2 2h-2M6 14h12v7H6v-7z'),
    (MODULE_STAFF, 'accounts:staff_list', _('Staff & access'), 'M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zM23 21v-2a4 4 0 00-3-3.87'),
    (MODULE_SETTINGS, 'reports:settings_home', _('Settings'), 'M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 008 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H2a2 2 0 110-4h.09A1.65 1.65 0 004.6 8a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 3.6 1.65 1.65 0 0010 2.09V2a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 8V8a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z'),
]


def allowed_items(user, tenant, path=''):
    """The nav entries this person may actually open, in order."""
    from .models import FeatureFlag

    if user is None or not user.is_authenticated or tenant is None:
        return []
    flags = dict(FeatureFlag.objects.filter(tenant=tenant).values_list('module', 'enabled'))
    items = []
    for module, url_name, label, icon in NAV_ITEMS:
        if module is not None and (not flags.get(module, True)
                                   or not user.can(module, 'view')):
            continue
        try:
            url = reverse(url_name)
        except Exception:
            continue
        items.append({
            'label': label,
            'url': url,
            'icon': icon,
            'active': path == url or (url != '/' and path.startswith(url)),
        })
    return items


def landing_url(user, tenant):
    """The first screen this person can open.

    A staff member with no reports grant used to be dropped on the dashboard,
    which refused them — an error page as the first thing they saw after signing
    in, with no way through to the modules they do have.
    """
    if user is None or not user.is_authenticated:
        return reverse('accounts:login')
    if user.is_super_admin and tenant is None:
        return reverse('platform:tenant_list')
    items = allowed_items(user, tenant)
    return items[0]['url'] if items else reverse('accounts:profile')


def build(request):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return []
    return allowed_items(user, getattr(request, 'tenant', None), request.path)
