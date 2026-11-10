"""Links that bring the user back to the page they started from.

A create link carries the current page as ``?next=``; the create view sends
the user back there after saving (see ``core.utils.redirect_back``).

    <a href="{% url 'guests:guest_create' %}?{% return_here 'guest' %}">
    <a href="{% url 'billing:payment_create' %}?invoice={{ i.pk }}&{% return_here %}">
    <a class="btn" href="{% back_or 'guests:guest_list' %}">Cancel</a>
"""
from django import template
from django.http import QueryDict
from django.urls import reverse

from core.utils import safe_next

register = template.Library()


@register.simple_tag(takes_context=True)
def return_here(context, pick=''):
    """``next=<this page>``, plus ``pick=<field>`` to have the new record chosen here."""
    request = context.get('request')
    if request is None:
        return ''
    params = QueryDict(mutable=True)
    params['next'] = request.get_full_path()
    if pick:
        params['pick'] = pick
    return params.urlencode()


@register.simple_tag(takes_context=True)
def back_or(context, viewname, *args):
    """Where Cancel goes: the page the user came from, else ``viewname``."""
    request = context.get('request')
    return (safe_next(request) if request else '') or reverse(viewname, args=args)
