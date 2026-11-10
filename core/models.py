"""Tenant, status, sequence, branding and audit primitives shared by every module."""
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Tenant(TimeStampedModel):
    """A client of the platform — one building owner / company."""

    STATUS_ACTIVE = 'active'
    STATUS_LOCKED = 'locked'
    STATUS_SUSPENDED = 'suspended'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, _('Active')),
        (STATUS_LOCKED, _('Locked (non-payment)')),
        (STATUS_SUSPENDED, _('Suspended')),
    ]

    name = models.CharField(_('company name'), max_length=150)
    slug = models.SlugField(unique=True)
    contact_person = models.CharField(max_length=120, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)

    currency = models.CharField(max_length=8, default='AED')
    date_format = models.CharField(max_length=32, default='d M Y')
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # Per-client limits, owned by the super admin.
    max_buildings = models.PositiveIntegerField(default=5)
    max_rooms = models.PositiveIntegerField(default=200)
    max_beds = models.PositiveIntegerField(default=1000)
    max_staff_seats = models.PositiveIntegerField(default=10)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def is_usable(self):
        return self.status == self.STATUS_ACTIVE

    def limit_report(self):
        from properties.models import Bed, Building, Room
        from accounts.models import User
        return [
            (_('Buildings'), Building.objects.filter(tenant=self).count(), self.max_buildings),
            (_('Rooms'), Room.objects.filter(tenant=self).count(), self.max_rooms),
            (_('Beds'), Bed.objects.filter(tenant=self).count(), self.max_beds),
            (_('Staff seats'), User.objects.filter(tenant=self, role=User.ROLE_STAFF, is_active=True).count(),
             self.max_staff_seats),
        ]


class Subscription(TimeStampedModel):
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name='subscription')
    plan = models.CharField(max_length=60, default='Standard')
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    billing_cycle = models.CharField(
        max_length=16,
        choices=[('monthly', _('Monthly')), ('yearly', _('Yearly'))],
        default='monthly',
    )
    starts_on = models.DateField(default=timezone.localdate)
    expires_on = models.DateField()
    grace_days = models.PositiveIntegerField(default=7)
    auto_lock = models.BooleanField(default=True)
    last_paid_on = models.DateField(null=True, blank=True)

    def __str__(self):
        return f'{self.tenant} — {self.plan}'

    @property
    def grace_end(self):
        return self.expires_on + timezone.timedelta(days=self.grace_days)

    @property
    def is_expired(self):
        return timezone.localdate() > self.expires_on

    @property
    def is_past_grace(self):
        return timezone.localdate() > self.grace_end

    @property
    def days_left(self):
        return (self.expires_on - timezone.localdate()).days


class TenantScopedManager(models.Manager):
    def for_tenant(self, tenant):
        if tenant is None:
            return self.get_queryset()
        return self.get_queryset().filter(tenant=tenant)


class TenantScopedModel(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='%(class)s_set')

    objects = TenantScopedManager()

    class Meta:
        abstract = True


class Status(TimeStampedModel):
    """Admin-editable status values with colour codes and behaviour flags.

    A row with ``tenant = None`` is a platform-level template that gets copied
    into every new tenant, so adding a status never needs a code change.
    """

    KIND_BED = 'bed'
    KIND_TENANCY = 'tenancy'
    KIND_INVOICE = 'invoice'
    KIND_CHOICES = [
        (KIND_BED, _('Bed status')),
        (KIND_TENANCY, _('Tenancy status')),
        (KIND_INVOICE, _('Invoice status')),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='statuses', null=True, blank=True
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    code = models.SlugField(help_text=_('Stable identifier used by automations, e.g. "occupied".'))
    name = models.CharField(max_length=60)
    color = models.CharField(max_length=9, default='#64748b', help_text=_('Hex colour, e.g. #16a34a'))
    counts_as_available = models.BooleanField(default=False)
    counts_as_occupied = models.BooleanField(default=False)
    counts_as_settled = models.BooleanField(
        default=False, help_text=_('Invoice statuses only — treat as fully settled.')
    )
    is_default = models.BooleanField(default=False, help_text=_('Applied to new records of this kind.'))
    is_protected = models.BooleanField(
        default=False, help_text=_('Seeded status the system relies on; cannot be deleted.')
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['kind', 'sort_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'kind', 'code'], name='uniq_status_per_tenant'),
        ]
        verbose_name_plural = _('statuses')

    def __str__(self):
        return f'{self.get_kind_display()}: {self.name}'

    @classmethod
    def default_for(cls, tenant, kind):
        qs = cls.objects.filter(tenant=tenant, kind=kind)
        return qs.filter(is_default=True).first() or qs.first()

    @classmethod
    def by_code(cls, tenant, kind, code):
        return cls.objects.filter(tenant=tenant, kind=kind, code=code).first()


class Sequence(models.Model):
    """Gap-free, never-reused counters for receipt and invoice numbers."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='sequences')
    key = models.CharField(max_length=40)
    prefix = models.CharField(max_length=12, blank=True)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'key'], name='uniq_sequence_per_tenant'),
        ]

    def __str__(self):
        return f'{self.tenant} / {self.key} = {self.last_value}'

    @classmethod
    def next_value(cls, tenant, key, prefix=''):
        """Atomically reserve the next number. Numbers are never reused."""
        from django.db import transaction

        with transaction.atomic():
            seq, _created = cls.objects.select_for_update().get_or_create(
                tenant=tenant, key=key, defaults={'prefix': prefix}
            )
            seq.last_value += 1
            if prefix and not seq.prefix:
                seq.prefix = prefix
            seq.save(update_fields=['last_value', 'prefix'])
            return f'{seq.prefix}{timezone.localdate():%Y%m}-{seq.last_value:05d}'


class Branding(TimeStampedModel):
    """Per-client look and feel, owned by the super admin."""

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name='branding')
    display_name = models.CharField(max_length=120, blank=True)
    logo = models.ImageField(upload_to='branding/', blank=True, null=True)
    primary_color = models.CharField(max_length=9, default='#0f3f52')
    accent_color = models.CharField(max_length=9, default='#e0a03c')
    login_tagline = models.CharField(max_length=160, blank=True)
    footer_note = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f'Branding for {self.tenant}'


class IntegrationSettings(TimeStampedModel):
    """Gateway credentials, held by the platform owner for one client.

    The super admin sets these per admin account from the platform console; the
    client's own admin never sees them. Values here win over the environment, so
    a client can be switched on without a deploy. They are stored as written —
    a database backup therefore carries live credentials and must be protected
    accordingly (see docs/RUNBOOK.md).
    """

    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name='integrations')

    # --- WhatsApp Business Cloud API ---
    whatsapp_enabled = models.BooleanField(
        _('send WhatsApp automatically'), default=False,
        help_text=_('Off means staff still open each message in WhatsApp by hand.'),
    )
    whatsapp_api_url = models.CharField(
        _('API base URL'), max_length=200, blank=True,
        default='https://graph.facebook.com/v21.0')
    whatsapp_phone_number_id = models.CharField(
        _('phone number ID'), max_length=60, blank=True)
    whatsapp_business_id = models.CharField(
        _('business account ID'), max_length=60, blank=True)
    whatsapp_access_token = models.TextField(_('access token'), blank=True)

    # --- Email / SMTP ---
    email_enabled = models.BooleanField(_('send email'), default=False)
    email_host = models.CharField(_('SMTP host'), max_length=120, blank=True)
    email_port = models.PositiveIntegerField(_('SMTP port'), default=587)
    email_use_tls = models.BooleanField(_('use TLS'), default=True)
    email_username = models.CharField(_('SMTP username'), max_length=150, blank=True)
    email_password = models.CharField(_('SMTP password'), max_length=200, blank=True)
    email_from = models.CharField(
        _('from address'), max_length=150, blank=True,
        help_text=_('What the guest sees the email coming from.'))

    # --- SMS gateway ---
    sms_enabled = models.BooleanField(_('send SMS'), default=False)
    sms_provider = models.CharField(_('provider'), max_length=60, blank=True)
    sms_api_key = models.CharField(_('API key'), max_length=200, blank=True)
    sms_sender_id = models.CharField(_('sender ID'), max_length=40, blank=True)

    # --- Payment gateway ---
    payment_provider = models.CharField(_('provider'), max_length=60, blank=True)
    payment_public_key = models.CharField(_('public key'), max_length=200, blank=True)
    payment_secret_key = models.CharField(_('secret key'), max_length=200, blank=True)

    #: Never rendered back to the screen; blank on the form means "leave as is".
    SECRET_FIELDS = ['whatsapp_access_token', 'email_password', 'sms_api_key',
                     'payment_secret_key']

    class Meta:
        verbose_name_plural = _('integration settings')

    def __str__(self):
        return f'{_("Integrations for")} {self.tenant}'

    def tail(self, field):
        """The last four characters, so a human can tell which key is stored."""
        value = (getattr(self, field, '') or '').strip()
        if not value:
            return ''
        return '••••' + value[-4:] if len(value) > 4 else '••••'

    def whatsapp_ready(self):
        return bool(self.whatsapp_enabled and self.whatsapp_phone_number_id
                    and self.whatsapp_access_token)

    def email_ready(self):
        return bool(self.email_enabled and self.email_host and self.email_from)


def whatsapp_config(tenant):
    """What the client is set up with, falling back to the server's environment."""
    from django.conf import settings as django_settings

    base = dict(django_settings.WHATSAPP)
    row = getattr(tenant, 'integrations', None) if tenant else None
    if row is None:
        return base
    if row.whatsapp_api_url:
        base['API_URL'] = row.whatsapp_api_url
    if row.whatsapp_phone_number_id:
        base['PHONE_NUMBER_ID'] = row.whatsapp_phone_number_id
    if row.whatsapp_access_token:
        base['ACCESS_TOKEN'] = row.whatsapp_access_token
    # A client is only switched on deliberately, never by the server default.
    base['ENABLED'] = row.whatsapp_ready()
    return base


class FeatureFlag(models.Model):
    """Super admin switch to turn a module on or off for one client."""

    MODULES = [
        ('properties', _('Property setup')),
        ('guests', _('Guests & tenancy')),
        ('billing', _('Rent & invoicing')),
        ('accounting', _('Income & expense')),
        ('messaging', _('WhatsApp alerts')),
        ('reports', _('Dashboard & reports')),
        ('pos', _('POS receipt printing')),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='feature_flags')
    module = models.CharField(max_length=32, choices=MODULES)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'module'], name='uniq_flag_per_tenant'),
        ]

    def __str__(self):
        return f'{self.tenant} / {self.module} = {self.enabled}'


class SystemNotice(TimeStampedModel):
    """Maintenance banner / release note published by the super admin."""

    title = models.CharField(max_length=160)
    body = models.TextField(blank=True)
    level = models.CharField(
        max_length=16,
        choices=[('info', _('Info')), ('warning', _('Warning')), ('maintenance', _('Maintenance'))],
        default='info',
    )
    is_active = models.BooleanField(default=True)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True,
        related_name='notices', help_text=_('Leave empty to show to every client.'),
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class AuditLog(models.Model):
    """Who changed what, when — including deletions and rent-amount edits."""

    ACTION_CREATE = 'create'
    ACTION_UPDATE = 'update'
    ACTION_DELETE = 'delete'
    ACTION_LOGIN = 'login'
    ACTION_LOGOUT = 'logout'
    ACTION_REVERSE = 'reverse'
    ACTION_IMPERSONATE = 'impersonate'
    ACTION_EXPORT = 'export'
    ACTION_PRINT = 'print'
    ACTION_CHOICES = [
        (ACTION_CREATE, _('Created')),
        (ACTION_UPDATE, _('Updated')),
        (ACTION_DELETE, _('Deleted')),
        (ACTION_LOGIN, _('Logged in')),
        (ACTION_LOGOUT, _('Logged out')),
        (ACTION_REVERSE, _('Reversed')),
        (ACTION_IMPERSONATE, _('Impersonated')),
        (ACTION_EXPORT, _('Exported')),
        (ACTION_PRINT, _('Printed')),
    ]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs'
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='audit_entries',
    )
    actor_label = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    model_name = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=40, blank=True)
    object_label = models.CharField(max_length=200, blank=True)
    summary = models.CharField(max_length=255, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.actor_label} {self.action} {self.object_label}'
