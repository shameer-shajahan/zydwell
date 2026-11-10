"""WhatsApp templates, scheduling rules and the delivery log."""
import re

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import TenantScopedModel


class MessageTemplate(TenantScopedModel):
    """A pre-approved WhatsApp Business template plus its local preview body."""

    CODE_CHOICES = [
        ('rent_reminder', _('Rent reminder')),
        ('rent_due_today', _('Rent due today')),
        ('rent_overdue', _('Rent overdue')),
        ('payment_confirmation', _('Payment confirmation')),
        ('warning_notice', _('Warning notice')),
        ('final_notice', _('Final notice before eviction')),
        ('document_expiry', _('Document / visa expiry')),
        ('checkout_settlement', _('Check-out & deposit settlement')),
        ('building_notice', _('Building notice to all guests')),
    ]

    code = models.SlugField(choices=CODE_CHOICES)
    name = models.CharField(max_length=100)
    language = models.CharField(max_length=8, default='en')
    whatsapp_template_name = models.CharField(
        max_length=100, blank=True,
        help_text=_('Name registered with the WhatsApp Business Cloud API.'),
    )
    body = models.TextField(
        help_text=_('Placeholders: {guest}, {building}, {bed}, {amount}, {due_date}, '
                    '{invoice}, {receipt}, {balance}, {days}, {document}, {expiry}, {link}'),
    )
    is_approved = models.BooleanField(
        default=False, help_text=_('Approved by Meta — required before sending.')
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['code', 'language']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'code', 'language'], name='uniq_template_per_tenant'
            ),
        ]

    def __str__(self):
        return f'{self.name} [{self.language}]'

    #: Placeholders that may legitimately be blank in a finished message.
    OPTIONAL_FIELDS = {'balance', 'reason', 'days', 'bed', 'document', 'expiry'}

    def placeholders(self):
        return set(re.findall(r'\{(\w+)\}', self.body))

    def missing_fields(self, context):
        """Placeholders this body uses that the caller has nothing to put in.

        Rendering them as empty produces messages like "your rent of AED  is
        due on ." — worse than not sending at all, because it goes to a guest.
        """
        return sorted(
            name for name in self.placeholders() - self.OPTIONAL_FIELDS
            if not str(context.get(name, '')).strip()
        )

    def render(self, context):
        body = self.body
        for key, value in context.items():
            body = body.replace('{%s}' % key, str(value if value is not None else ''))
        return body


class ReminderRule(TenantScopedModel):
    """When a rent reminder goes out, relative to the invoice due date."""

    offset_days = models.IntegerField(
        help_text=_('Negative is before the due date, 0 is on it, positive is after.')
    )
    template = models.ForeignKey(
        MessageTemplate, on_delete=models.CASCADE, related_name='reminder_rules'
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['offset_days']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'offset_days'], name='uniq_rule_per_offset'),
        ]

    def __str__(self):
        if self.offset_days < 0:
            return str(_('%(n)d days before due') % {'n': abs(self.offset_days)})
        if self.offset_days == 0:
            return str(_('On the due date'))
        return str(_('%(n)d days after due') % {'n': self.offset_days})


CHANNEL_API = 'cloud_api'
CHANNEL_CLICK = 'click_to_chat'


class MessageLog(TenantScopedModel):
    """Every message attempt, so staff can prove a reminder was issued.

    Two ways out: the Business API sends by itself, or a member of staff opens
    the message in WhatsApp and presses send. The second needs no API keys and
    no template approval, which is how most of this gets sent in practice.
    """

    STATUS_QUEUED = 'queued'
    STATUS_OPENED = 'opened'
    STATUS_SENT = 'sent'
    STATUS_DELIVERED = 'delivered'
    STATUS_READ = 'read'
    STATUS_FAILED = 'failed'
    STATUS_SKIPPED = 'skipped'
    STATUS_CHOICES = [
        (STATUS_QUEUED, _('Waiting to send')),
        (STATUS_OPENED, _('Opened in WhatsApp')),
        (STATUS_SENT, _('Sent')),
        (STATUS_DELIVERED, _('Delivered')),
        (STATUS_READ, _('Read')),
        (STATUS_FAILED, _('Failed')),
        (STATUS_SKIPPED, _('Skipped')),
    ]

    guest = models.ForeignKey(
        'guests.Guest', on_delete=models.SET_NULL, null=True, blank=True, related_name='messages'
    )
    tenancy = models.ForeignKey(
        'guests.Tenancy', on_delete=models.SET_NULL, null=True, blank=True, related_name='messages'
    )
    invoice = models.ForeignKey(
        'billing.Invoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='messages'
    )
    template = models.ForeignKey(
        MessageTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name='messages'
    )
    template_code = models.CharField(max_length=40, blank=True)
    channel = models.CharField(
        max_length=16,
        choices=[
            (CHANNEL_API, _('WhatsApp Business API')),
            (CHANNEL_CLICK, _('Opened in WhatsApp by staff')),
        ],
        default=CHANNEL_API,
    )
    to_number = models.CharField(max_length=32)
    body = models.TextField()
    attachment_name = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    provider_message_id = models.CharField(max_length=120, blank=True)
    error = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='messages_sent',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', 'created_at']),
            models.Index(fields=['tenant', 'status', 'created_at']),
        ]

    def __str__(self):
        return f'{self.to_number} {self.template_code} [{self.status}]'

    @property
    def is_waiting(self):
        return self.status == self.STATUS_QUEUED

    @property
    def was_handed_to_whatsapp(self):
        return self.status in (self.STATUS_OPENED, self.STATUS_SENT,
                               self.STATUS_DELIVERED, self.STATUS_READ)
