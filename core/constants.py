"""Module keys used by feature flags and per-staff permissions."""
from django.utils.translation import gettext_lazy as _

MODULE_PROPERTIES = 'properties'
MODULE_GUESTS = 'guests'
MODULE_BILLING = 'billing'
MODULE_ACCOUNTING = 'accounting'
MODULE_MESSAGING = 'messaging'
MODULE_REPORTS = 'reports'
MODULE_POS = 'pos'
MODULE_STAFF = 'staff'
MODULE_SETTINGS = 'settings'

PERMISSION_MODULES = [
    (MODULE_PROPERTIES, _('Property setup')),
    (MODULE_GUESTS, _('Guests & tenancy')),
    (MODULE_BILLING, _('Rent & invoicing')),
    (MODULE_ACCOUNTING, _('Income & expense')),
    (MODULE_MESSAGING, _('WhatsApp alerts')),
    (MODULE_REPORTS, _('Dashboard & reports')),
    (MODULE_POS, _('POS receipt printing')),
    (MODULE_STAFF, _('Staff accounts')),
    (MODULE_SETTINGS, _('Settings & statuses')),
]

ACTIONS = ['view', 'create', 'edit', 'delete', 'approve']

#: Modules a staff account may never be granted — admin-only by design.
ADMIN_ONLY_MODULES = {MODULE_STAFF, MODULE_SETTINGS}
