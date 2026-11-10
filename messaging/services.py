"""Sending WhatsApp messages and logging every attempt with its delivery status."""
import json
import re
from decimal import Decimal
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import whatsapp_config

from .models import CHANNEL_API, CHANNEL_CLICK, MessageLog, MessageTemplate

#: wa.me works on a phone and on a desktop; web.whatsapp.com skips the
#: "Continue to Chat" interstitial when there is already a browser session.
CHAT_URL_MOBILE = 'https://wa.me/{number}?text={text}'
CHAT_URL_WEB = 'https://web.whatsapp.com/send?phone={number}&text={text}'

DESKTOP_HINTS = ('windows nt', 'macintosh', 'x11', 'cros')
MOBILE_HINTS = ('iphone', 'ipad', 'android', 'mobile')

PHONE_CLEAN = re.compile(r'[^0-9+]')


def normalise_number(raw, default_country='971'):
    """Turn a locally-typed mobile into an E.164-ish number for the API."""
    digits = PHONE_CLEAN.sub('', raw or '')
    if not digits:
        return ''
    if digits.startswith('+'):
        return digits[1:]
    if digits.startswith('00'):
        return digits[2:]
    if digits.startswith('0'):
        return default_country + digits[1:]
    return digits


def prefers_whatsapp_web(request):
    """Desktop browsers go to WhatsApp Web; phones go to the app."""
    if request is None:
        return False
    agent = request.META.get('HTTP_USER_AGENT', '').lower()
    if any(hint in agent for hint in MOBILE_HINTS):
        return False
    return any(hint in agent for hint in DESKTOP_HINTS)


def chat_url(number, body, *, prefer_web=False):
    """A click-to-chat link that opens WhatsApp with the message ready to send."""
    number = normalise_number(number)
    if not number:
        return ''
    template = CHAT_URL_WEB if prefer_web else CHAT_URL_MOBILE
    return template.format(number=number, text=quote(body or '', safe=''))


def hand_to_whatsapp(*, tenant, template_code, to_number, context, guest=None,
                     tenancy=None, invoice=None, user=None, body_override='',
                     prefer_web=False):
    """Render, log, and return the URL that opens WhatsApp with it loaded.

    Nothing is transmitted from the server — a member of staff presses send in
    WhatsApp — so the log records it as opened rather than delivered.
    """
    template = MessageTemplate.objects.filter(
        tenant=tenant, code=template_code, language='en', is_active=True
    ).first()
    body = body_override or (template.render(context) if template else '')
    number = normalise_number(to_number)

    log = MessageLog.objects.create(
        tenant=tenant, guest=guest, tenancy=tenancy, invoice=invoice, template=template,
        template_code=template_code, to_number=number, body=body,
        channel=CHANNEL_CLICK, sent_by=user,
    )
    if not number:
        return _fail(log, _('No mobile or WhatsApp number on file.')), ''
    if not body:
        return _fail(log, _('Nothing to send — no template and no message text.')), ''
    gaps = _gaps(template, context, body_override)
    if gaps:
        return _fail(log, _('Nothing to fill in for %(fields)s — the message would '
                            'have gaps in it.') % {'fields': ', '.join(gaps)}), ''

    log.status = MessageLog.STATUS_OPENED
    log.sent_at = timezone.now()
    log.save(update_fields=['status', 'sent_at'])
    return log, chat_url(number, body, prefer_web=prefer_web)


def reopen(log, *, user=None, prefer_web=False):
    """Hand an already-logged message to WhatsApp, e.g. from the waiting list."""
    log.channel = CHANNEL_CLICK
    log.status = MessageLog.STATUS_OPENED
    log.sent_at = timezone.now()
    log.error = ''
    if user is not None:
        log.sent_by = user
    log.save(update_fields=['channel', 'status', 'sent_at', 'error', 'sent_by'])
    return chat_url(log.to_number, log.body, prefer_web=prefer_web)


def base_context(tenant, *, guest=None, tenancy=None, invoice=None, payment=None, extra=None):
    ctx = {
        'currency': tenant.currency,
        'guest': guest.full_name if guest else '',
        'building': tenancy.building.name if tenancy else tenant.name,
        'bed': tenancy.location_label() if tenancy else '',
        'amount': '', 'due_date': '', 'invoice': '', 'receipt': '', 'balance': '',
        'days': '', 'document': '', 'expiry': '', 'reason': '', 'message': '',
        'link': '',
    }
    if invoice is not None:
        ctx.update({
            'amount': f'{invoice.balance:,.2f}',
            'due_date': invoice.due_date.strftime('%d-%m-%Y'),
            'invoice': invoice.number,
            'days': str(invoice.days_overdue),
        })
    if payment is not None:
        ctx.update({
            'amount': f'{payment.amount:,.2f}',
            'receipt': payment.receipt_number,
            'balance': f'{(payment.invoice.balance if payment.invoice else Decimal("0")):,.2f}',
        })
    if extra:
        ctx.update(extra)
    return ctx


def send(*, tenant, template_code, to_number, context, guest=None, tenancy=None, invoice=None,
         user=None, attachment_name='', body_override=''):
    """Render, send and log. Returns the ``MessageLog`` row either way."""
    template = MessageTemplate.objects.filter(
        tenant=tenant, code=template_code, language='en', is_active=True
    ).first()
    body = body_override or (template.render(context) if template else '')
    number = normalise_number(to_number)

    log = MessageLog.objects.create(
        tenant=tenant, guest=guest, tenancy=tenancy, invoice=invoice, template=template,
        template_code=template_code, to_number=number, body=body,
        attachment_name=attachment_name, sent_by=user, channel=CHANNEL_API,
    )

    if not number:
        return _fail(log, _('No mobile number on file.'))
    if not body:
        return _fail(log, _('No active template for %(code)s.') % {'code': template_code})
    gaps = _gaps(template, context, body_override)
    if gaps:
        return _fail(log, _('Nothing to fill in for %(fields)s — the message would '
                            'have gaps in it.') % {'fields': ', '.join(gaps)})
    if template and not template.is_approved:
        log.status = MessageLog.STATUS_SKIPPED
        log.error = _('Template not yet approved by Meta.')
        log.save(update_fields=['status', 'error'])
        return log
    if not whatsapp_config(tenant)['ENABLED']:
        log.status = MessageLog.STATUS_QUEUED
        log.error = _('Automatic sending is off — waiting for someone to open it '
                      'in WhatsApp.')
        log.save(update_fields=['status', 'error'])
        return log

    return _dispatch(log, template)


def api_post(url, headers, payload):
    """One place for the outbound call, so tests can stand in for the network."""
    import requests

    return requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)


def _dispatch(log, template):

    cfg = whatsapp_config(log.tenant)
    url = f"{cfg['API_URL']}/{cfg['PHONE_NUMBER_ID']}/messages"
    payload = {
        'messaging_product': 'whatsapp',
        'to': log.to_number,
        'type': 'text',
        'text': {'body': log.body},
    }
    if template and template.whatsapp_template_name:
        payload = {
            'messaging_product': 'whatsapp',
            'to': log.to_number,
            'type': 'template',
            'template': {
                'name': template.whatsapp_template_name,
                'language': {'code': template.language},
                'components': [{
                    'type': 'body',
                    'parameters': [{'type': 'text', 'text': log.body}],
                }],
            },
        }
    try:
        response = api_post(url, {'Authorization': f"Bearer {cfg['ACCESS_TOKEN']}",
                                  'Content-Type': 'application/json'}, payload)
        if response.status_code < 300:
            data = response.json()
            log.status = MessageLog.STATUS_SENT
            log.provider_message_id = (data.get('messages') or [{}])[0].get('id', '')
            log.sent_at = timezone.now()
            log.save(update_fields=['status', 'provider_message_id', 'sent_at'])
        else:
            _fail(log, f'HTTP {response.status_code}: {response.text[:200]}')
    except Exception as exc:  # network, DNS, timeout — never break the caller
        _fail(log, str(exc)[:250])
    return log


def _gaps(template, context, body_override=''):
    """Placeholders left empty. A hand-written message is the sender's business."""
    if body_override or template is None:
        return []
    return template.missing_fields(context)


def _fail(log, message):
    log.status = MessageLog.STATUS_FAILED
    log.error = str(message)[:255]
    log.save(update_fields=['status', 'error'])
    return log


def send_payment_confirmation(payment, user=None):
    tenancy = payment.tenancy
    return send(
        tenant=payment.tenant,
        template_code='payment_confirmation',
        to_number=tenancy.guest.messaging_number(),
        context=base_context(payment.tenant, guest=tenancy.guest, tenancy=tenancy,
                             payment=payment),
        guest=tenancy.guest, tenancy=tenancy, invoice=payment.invoice, user=user,
        attachment_name=f'{payment.receipt_number}.pdf',
    )


# --- Keeping the log short ----------------------------------------------------
def purge_old_messages(tenant=None, days=None):
    """Delete log entries older than ``days`` (default 30). Returns how many went.

    A message still waiting to be sent is kept however old it is — deleting it
    would quietly cancel a reminder nobody has sent yet.
    """
    from core import audit
    from core.models import AuditLog, Tenant

    days = settings.ZYDWELL['MESSAGE_LOG_DAYS'] if days is None else days
    cutoff = timezone.now() - timezone.timedelta(days=days)
    rows = MessageLog.objects.filter(created_at__lt=cutoff).exclude(
        status=MessageLog.STATUS_QUEUED)
    owners = [tenant] if tenant is not None else \
        Tenant.objects.filter(pk__in=rows.values('tenant_id'))
    removed = 0
    for owner in owners:
        count = rows.filter(tenant=owner).delete()[1].get(MessageLog._meta.label, 0)
        removed += count
        if count:
            audit.log(AuditLog.ACTION_DELETE, None, tenant=owner,
                      model_name='MessageLog', object_label='whatsapp-log',
                      summary=_('Automatic clean-up: deleted %(n)d WhatsApp log entries '
                                'older than %(days)d days') % {'n': count, 'days': days})
    return removed


def purge_old_messages_daily(tenant):
    """Run the clean-up at most once a day per client, from whichever screen
    gets there first — so it happens even where the nightly job is not set up."""
    key = f'zydwell:message-purge:{tenant.pk}:{timezone.localdate():%Y%m%d}'
    if not cache.add(key, 1, 60 * 60 * 26):
        return 0
    return purge_old_messages(tenant)
