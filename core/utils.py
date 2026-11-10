"""Small shared helpers for views."""
import re
from datetime import date
from urllib.parse import urlsplit, urlunsplit

from django.core.paginator import Paginator
from django.http import QueryDict
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

_PICK_NAME = re.compile(r'[a-z_]{1,30}')


def safe_next(request):
    """The page to go back to, when ``?next=`` names one on this site.

    Only a local path is accepted, so a crafted link cannot send someone off to
    another site after they save.
    """
    for source in (request.POST, request.GET):
        url = (source.get('next') or '').strip()
        if url.startswith('/') and url_has_allowed_host_and_scheme(
                url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            return url
    return ''


def redirect_back(request, fallback, *args, made=None, **kwargs):
    """Back to the page the user came from, else to ``fallback``.

    ``made`` is the record just saved. When the page that sent the user here
    asked for it with ``?pick=<field>`` — check-in asking for a new guest — the
    record's id goes back under that name, so the form there has it chosen.
    """
    url = safe_next(request)
    if not url:
        return redirect(fallback, *args, **kwargs)
    pick = request.POST.get('pick') or request.GET.get('pick') or ''
    if made is not None and _PICK_NAME.fullmatch(pick):
        parts = urlsplit(url)
        params = QueryDict(parts.query, mutable=True)
        params[pick] = str(made.pk)
        url = urlunsplit(parts._replace(query=params.urlencode()))
    return redirect(url)


def paginate(request, queryset, per_page=25, param='page'):
    """One page of ``queryset``. Pages showing two lists give each its own ``param``."""
    paginator = Paginator(queryset, per_page)
    page = paginator.get_page(request.GET.get(param))
    page.param = param
    return page


def month_bounds(value=None):
    """First and last day of the month containing ``value`` (default today)."""
    value = value or timezone.localdate()
    start = value.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1) - timezone.timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1) - timezone.timedelta(days=1)
    return start, end


def parse_date(raw, fallback=None):
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return fallback


def date_range_from_request(request, default_days=None):
    """``(start, end)`` honouring ?from= and ?to=, defaulting to this month."""
    today = timezone.localdate()
    if default_days:
        default_start, default_end = today - timezone.timedelta(days=default_days), today
    else:
        default_start, default_end = month_bounds(today)
    start = parse_date(request.GET.get('from'), default_start)
    end = parse_date(request.GET.get('to'), default_end)
    if start > end:
        start, end = end, start
    return start, end


def last_n_months(count=12, end=None):
    """A list of ``(year, month, label, start, end)`` oldest first."""
    end = end or timezone.localdate()
    out = []
    year, month = end.year, end.month
    for _step in range(count):
        start = date(year, month, 1)
        finish = month_bounds(start)[1]
        out.append((year, month, start.strftime('%b %y'), start, finish))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(out))
