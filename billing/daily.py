"""Raise the day's invoices once a day, without a server cron job.

The hosting starts only the web server, so the nightly command may never run.
Instead the first request of the day for an account bills it: due invoices
(every missed period too) and late fees — what "Run billing" does. A row
lock on the account keeps two server workers from billing it twice, and a
memo per process saves even the date check on every later request.
"""
import logging

from django.db import transaction
from django.utils import timezone

from core.models import Tenant

from .services import apply_late_fees, generate_due_invoices

logger = logging.getLogger(__name__)
_done = {}


def bill_today(tenant):
    """Bill ``tenant`` if nothing has today. Returns the invoices raised."""
    today = timezone.localdate()
    if _done.get(tenant.pk) == today:
        return []
    with transaction.atomic():
        locked = Tenant.objects.select_for_update().get(pk=tenant.pk)
        if locked.billing_run_on == today:
            _done[tenant.pk] = today
            return []
        created = generate_due_invoices(locked, on_date=today)
        apply_late_fees(locked, on_date=today)
        Tenant.objects.filter(pk=locked.pk).update(billing_run_on=today)
    _done[tenant.pk] = today
    return created


def bill_today_quietly(tenant):
    """For a page request: never let billing trouble stop the page."""
    try:
        return bill_today(tenant)
    except Exception:          # logged, retried on the next request
        logger.exception('Daily billing failed for %s', tenant.pk)
        return []
