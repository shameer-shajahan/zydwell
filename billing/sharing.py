"""Signed, expiring links that let a guest open their own receipt.

A WhatsApp click-to-chat link can only carry text, never an attachment, so the
message carries a link instead. The token is signed rather than stored: no extra
table, nothing to clean up, and it stops working on its own.
"""
from django.conf import settings
from django.core import signing

SALT = 'zydwell.receipt.v1'
DEPOSIT_SALT = 'zydwell.deposit.v1'


def link_max_age():
    return settings.ZYDWELL.get('RECEIPT_LINK_DAYS', 30) * 24 * 3600


def receipt_token(payment):
    return signing.dumps({'p': payment.pk, 't': payment.tenant_id}, salt=SALT)


def payment_from_token(token):
    """The payment this token names, or ``None`` if it is bad or too old."""
    from .models import Payment

    try:
        data = signing.loads(token, salt=SALT, max_age=link_max_age())
    except signing.SignatureExpired:
        return None, 'expired'
    except signing.BadSignature:
        return None, 'invalid'

    payment = (
        Payment.objects.filter(pk=data.get('p'), tenant_id=data.get('t'))
        .select_related('tenancy__guest', 'tenancy__building', 'tenancy__room',
                        'tenancy__bed', 'invoice', 'tenant')
        .first()
    )
    if payment is None:
        return None, 'invalid'
    return payment, ''


def receipt_url(payment, request=None):
    from django.urls import reverse

    path = reverse('public_receipt', args=[receipt_token(payment)])
    return request.build_absolute_uri(path) if request else path


def deposit_token(entry):
    return signing.dumps({'d': entry.pk, 't': entry.tenant_id}, salt=DEPOSIT_SALT)


def deposit_from_token(token):
    from guests.models import DepositTransaction

    try:
        data = signing.loads(token, salt=DEPOSIT_SALT, max_age=link_max_age())
    except signing.SignatureExpired:
        return None, 'expired'
    except signing.BadSignature:
        return None, 'invalid'

    entry = (
        DepositTransaction.objects.filter(pk=data.get('d'), tenant_id=data.get('t'))
        .select_related('tenancy__guest', 'tenancy__building', 'tenancy__room',
                        'tenancy__bed', 'tenant', 'recorded_by')
        .first()
    )
    if entry is None:
        return None, 'invalid'
    return entry, ''


def deposit_url(entry, request=None):
    from django.urls import reverse

    path = reverse('public_deposit_receipt', args=[deposit_token(entry)])
    return request.build_absolute_uri(path) if request else path
