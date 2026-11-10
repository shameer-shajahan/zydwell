"""Helpers that record every meaningful change into the audit log."""
from decimal import Decimal

from django.forms.models import model_to_dict

from .current import get_current_request, get_current_user
from .models import AuditLog


def client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def log(action, obj=None, *, tenant=None, summary='', changes=None, actor=None, request=None,
        model_name='', object_label=''):
    request = request or get_current_request()
    actor = actor or get_current_user()
    if tenant is None:
        tenant = getattr(obj, 'tenant', None) or getattr(request, 'tenant', None)
    if obj is not None:
        model_name = model_name or obj.__class__.__name__
        object_label = object_label or str(obj)[:200]
    return AuditLog.objects.create(
        tenant=tenant,
        actor=actor,
        actor_label=(actor.get_full_name() or actor.get_username()) if actor else 'system',
        action=action,
        model_name=model_name,
        object_id=str(getattr(obj, 'pk', '') or ''),
        object_label=object_label,
        summary=summary[:255],
        changes=changes or {},
        ip_address=client_ip(request),
        user_agent=(request.META.get('HTTP_USER_AGENT', '')[:255] if request else ''),
    )


def snapshot(instance, fields=None):
    """A comparable dict of an instance's current field values."""
    if instance is None or instance.pk is None:
        return {}
    data = model_to_dict(instance, fields=fields)
    return {k: _plain(v) for k, v in data.items()}


def diff(before, after):
    """Field-by-field ``{field: [old, new]}`` for the values that moved."""
    out = {}
    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if old != new:
            out[key] = [old, new]
    return out


def _plain(value):
    if isinstance(value, Decimal):
        # Normalise so 800.00 and 800 compare equal — a form-bound Decimal and a
        # database-loaded one otherwise look like a change when nothing moved.
        return f'{value.normalize():f}'
    if hasattr(value, 'pk'):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
