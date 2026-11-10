"""Links that keep a list's filters while changing one of them."""
from django import template

from core.filters import LEVELS

register = template.Library()


@register.simple_tag(takes_context=True)
def query(context, *drop, **changes):
    """``?…`` built from the current query string.

    Page counters are always dropped — a different filter is a different list,
    so it starts again at page one. ``drop`` names parameters to remove
    (``'location'`` removes every level of the location picker) and keyword
    arguments set or, when empty, remove one.

        <a href="{% query state='current' %}">       keep the rest, change state
        <a href="{% query 'location' %}">             clear the location picker
    """
    params = context['request'].GET.copy()
    for key in list(params):
        if key == 'page' or key.endswith('_page'):
            params.pop(key)
    for name in drop:
        for key in (LEVELS if name == 'location' else (name,)):
            params.pop(key, None)
    for key, value in changes.items():
        if value in (None, ''):
            params.pop(key, None)
        else:
            params[key] = str(value)
    encoded = params.urlencode()
    return f'?{encoded}' if encoded else '?'

