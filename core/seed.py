"""Default master data pushed into every new client account."""
from decimal import Decimal

from django.utils import timezone

from .models import Branding, FeatureFlag, Status, Subscription

BED_STATUSES = [
    # code, name, colour, available, occupied, default
    ('available', 'Available', '#16a34a', True, False, True),
    ('occupied', 'Occupied', '#dc2626', False, True, False),
    ('reserved', 'Reserved / Advance paid', '#e0a03c', False, True, False),
    ('maintenance', 'Under maintenance', '#64748b', False, False, False),
    ('blocked', 'Blocked', '#1e293b', False, False, False),
]

TENANCY_STATUSES = [
    ('active', 'Active', '#16a34a', True),
    ('notice', 'Notice period', '#e0a03c', False),
    ('vacated', 'Vacated', '#64748b', False),
]

INVOICE_STATUSES = [
    # code, name, colour, settled, default
    ('paid', 'Paid', '#16a34a', True, False),
    ('partly_paid', 'Partly paid', '#0d7d8c', False, False),
    ('pending', 'Pending', '#e0a03c', False, True),
    ('overdue', 'Overdue', '#dc2626', False, False),
    ('cancelled', 'Cancelled', '#475569', True, False),
]

INCOME_CATEGORIES = [
    ('monthly_rent', 'Monthly rent', True),
    ('security_deposit', 'Security deposit', True),
    ('late_payment_fee', 'Late payment fee', True),
    ('utilities_share', 'Laundry / Wi-Fi / utilities share', False),
    ('key_replacement', 'Key or card replacement', False),
    ('other_income', 'Other income', False),
]

EXPENSE_CATEGORIES = [
    ('electricity_water', 'Electricity and water (DEWA)', False),
    ('building_rent', 'Building rent or mortgage', False),
    ('maintenance', 'Maintenance and repairs', False),
    ('cleaning', 'Cleaning and consumables', False),
    ('staff_salary', 'Staff salary', False),
    ('internet_licence', 'Internet, municipality and licence fees', False),
    ('other_expense', 'Other expense', False),
]

PAYMENT_MODES = [
    # code, name, cash, reference, due date, from credit
    ('cash', 'Cash', True, False, False, False),
    ('card', 'Card', False, True, False, False),
    ('upi', 'UPI', False, True, False, False),
    ('bank', 'Bank transfer', False, True, False, False),
    ('cheque', 'Cheque', False, True, True, False),
    ('credit', 'Guest credit', False, False, False, True),
    ('online', 'Online / link', False, True, False, False),
]

ROOM_TYPES = [
    ('bedspace', 'Bedspace / sharing room', 'Several beds let individually'),
    ('partition', 'Partition', 'Partitioned space within a room'),
    ('studio', 'Studio', 'Self-contained studio unit'),
    ('family_flat', 'Family flat / full apartment', 'Whole apartment let to one party'),
]

MESSAGE_TEMPLATES = [
    ('rent_reminder', 'Rent reminder (before due)',
     'Dear {guest}, your rent of {currency} {amount} for {bed} is due on {due_date}. '
     'Please pay on time. — {building}'),
    ('rent_due_today', 'Rent due today',
     'Dear {guest}, your rent of {currency} {amount} for {bed} is due today ({due_date}). '
     'Invoice {invoice}. — {building}'),
    ('rent_overdue', 'Rent overdue',
     'Dear {guest}, rent of {currency} {amount} for {bed} is now {days} days overdue. '
     'Kindly settle invoice {invoice} immediately. — {building}'),
    ('payment_confirmation', 'Payment confirmation',
     'Thank you {guest}. We received {currency} {amount}. Receipt {receipt}. '
     'Remaining balance: {currency} {balance}. — {building}'),
    ('warning_notice', 'Warning notice',
     'Dear {guest}, this is a formal warning regarding {reason}. Please contact the '
     'supervisor at {building} within 48 hours.'),
    ('final_notice', 'Final notice before eviction',
     'Dear {guest}, despite earlier reminders, {currency} {amount} remains unpaid for {days} '
     'days. This is the final notice before eviction proceedings. — {building}'),
    ('document_expiry', 'Document / visa expiry',
     'Dear {guest}, your {document} expires on {expiry} ({days} days left). '
     'Please share a renewed copy with the office. — {building}'),
    ('checkout_settlement', 'Check-out & deposit settlement',
     'Dear {guest}, your check-out from {bed} is complete. Deposit settlement: '
     '{currency} {amount}. Thank you for staying with {building}.'),
    ('building_notice', 'Building notice',
     'Notice from {building}: {message}'),
    ('receipt_share', 'Receipt with PDF link',
     'Thank you {guest}. We received {currency} {amount} for {bed}. '
     'Receipt {receipt}: {link} — {building}'),
    ('profile_link', 'Complete your profile',
     'Hello {guest}, please complete your details for {building} here: {link} '
     'The link works for {days} more day(s).'),
]

REMINDER_OFFSETS = [(-5, 'rent_reminder'), (0, 'rent_due_today'), (3, 'rent_overdue'),
                    (7, 'rent_overdue'), (15, 'final_notice')]


def seed_platform_status_templates():
    """Global status rows (tenant=None) that act as the factory defaults."""
    _write_statuses(None)


def seed_tenant(tenant, *, with_subscription=True):
    """Give a brand-new client every default it needs to be usable."""
    from accounting.models import KIND_EXPENSE, KIND_INCOME, Category
    from billing.models import LateFeeRule, PaymentMode, PrinterConfig
    from messaging.models import MessageTemplate, ReminderRule
    from properties.models import RoomType

    _write_statuses(tenant)

    for order, (code, name, is_system) in enumerate(INCOME_CATEGORIES):
        Category.objects.get_or_create(
            tenant=tenant, kind=KIND_INCOME, code=code,
            defaults={'name': name, 'is_system': is_system, 'sort_order': order},
        )
    for order, (code, name, is_system) in enumerate(EXPENSE_CATEGORIES):
        Category.objects.get_or_create(
            tenant=tenant, kind=KIND_EXPENSE, code=code,
            defaults={'name': name, 'is_system': is_system, 'sort_order': order},
        )

    for order, (code, name, cash, reference, due, credit) in enumerate(PAYMENT_MODES):
        PaymentMode.objects.get_or_create(
            tenant=tenant, code=code,
            defaults={'name': name, 'counts_as_cash': cash, 'needs_reference': reference,
                      'needs_due_date': due, 'settles_from_credit': credit,
                      'sort_order': order},
        )

    for order, (code, name, description) in enumerate(ROOM_TYPES):
        RoomType.objects.get_or_create(
            tenant=tenant, code=code,
            defaults={'name': name, 'description': description, 'sort_order': order},
        )

    templates = {}
    for code, name, body in MESSAGE_TEMPLATES:
        obj, _created = MessageTemplate.objects.get_or_create(
            tenant=tenant, code=code, language='en',
            defaults={'name': name, 'body': body, 'whatsapp_template_name': f'zydwell_{code}'},
        )
        templates[code] = obj

    for offset, code in REMINDER_OFFSETS:
        if code in templates:
            ReminderRule.objects.get_or_create(
                tenant=tenant, offset_days=offset,
                defaults={'template': templates[code], 'is_active': True},
            )

    LateFeeRule.objects.get_or_create(
        tenant=tenant, name='Standard late fee',
        defaults={'grace_days': 5, 'kind': LateFeeRule.KIND_FIXED, 'value': Decimal('50'),
                  'is_active': False},
    )
    PrinterConfig.objects.get_or_create(
        tenant=tenant, name='Front desk thermal',
        defaults={'paper_width': PrinterConfig.WIDTH_80,
                  'connection': PrinterConfig.CONNECTION_NETWORK,
                  'address': '192.168.1.50:9100', 'is_default': True,
                  'footer_text': 'Thank you. This receipt is computer generated.'},
    )
    Branding.objects.get_or_create(tenant=tenant, defaults={'display_name': tenant.name})
    for module, _label in FeatureFlag.MODULES:
        FeatureFlag.objects.get_or_create(tenant=tenant, module=module, defaults={'enabled': True})

    if with_subscription and not hasattr(tenant, 'subscription'):
        today = timezone.localdate()
        Subscription.objects.create(
            tenant=tenant, plan='Standard', price=Decimal('0'),
            starts_on=today, expires_on=today + timezone.timedelta(days=365),
        )
    return tenant


def _write_statuses(tenant):
    for order, (code, name, color, available, occupied, default) in enumerate(BED_STATUSES):
        Status.objects.get_or_create(
            tenant=tenant, kind=Status.KIND_BED, code=code,
            defaults={'name': name, 'color': color, 'counts_as_available': available,
                      'counts_as_occupied': occupied, 'is_default': default,
                      'is_protected': code in {'available', 'occupied'}, 'sort_order': order},
        )
    for order, (code, name, color, default) in enumerate(TENANCY_STATUSES):
        Status.objects.get_or_create(
            tenant=tenant, kind=Status.KIND_TENANCY, code=code,
            defaults={'name': name, 'color': color, 'is_default': default,
                      'is_protected': code in {'active', 'vacated'}, 'sort_order': order},
        )
    for order, (code, name, color, settled, default) in enumerate(INVOICE_STATUSES):
        Status.objects.get_or_create(
            tenant=tenant, kind=Status.KIND_INVOICE, code=code,
            defaults={'name': name, 'color': color, 'counts_as_settled': settled,
                      'is_default': default, 'is_protected': code in {'paid', 'pending'},
                      'sort_order': order},
        )
