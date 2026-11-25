"""Helpers several skills share: reading a message, and finding "who"."""
import re

from django.utils.translation import gettext as _

from .. import lookup
from ..brain import entities, parse
from .base import cancel_button, choose, say


RECENT = re.compile(
    r'\b(?:recently|just|newly|last|latest|new(?:est)?)\s+(?:added|created|registered|entered|'
    r'joined)\b|\b(?:last|latest|newest)\s+(?:guest|tenant|one|person)\b', re.I)


def read(ctx, text, *, tags=(), forward=True, place=False, building=False):
    """Everything a message holds, and the words left over for a name.

    ``place`` is ``True`` for "room 101 bed A" read by fixed patterns, or
    ``'codes'`` for every apartment, room and bed code in any order (the
    caller matches them against real beds, and plain numbers that fit no bed
    are left in ``codes`` for it to read as money).

    Order matters: a date ("30/11") or a room ("101-A") would otherwise be
    read as money, and a building's name as a guest's.
    """
    original = text or ''
    text = parse.normalise(original)
    found = {'text': text, 'original': original, 'today': ctx.today,
             'recent': bool(RECENT.search(text))}
    rest = text
    mobile = entities.extract(rest, name='marker').get('mobile', '')
    if mobile:
        found['mobile'] = mobile
    reference, rest = parse.find_reference(rest)
    if reference:
        found['reference'] = reference
    when, span = parse.find_date(rest, ctx.today, forward=forward)
    if when:
        found['date'] = when
        rest = parse.text_without(rest, [span])
    if building or place:
        bldg, span = lookup.find_building(ctx, rest)
        if bldg is not None:
            found['building'] = bldg
            rest = parse.text_without(rest, [span])
    if place == 'codes':
        tagged, _plain, rest = parse.find_money(rest, tags, plain=False)
        found['tagged'], found['amounts'] = tagged, []
        found['codes'], spans = parse.place_codes(rest)
        rest = parse.text_without(rest, spans)
    else:
        if place:
            spot, rest = parse.find_place(rest)
            found.update(spot)
        tagged, plain, rest = parse.find_money(rest, tags)
        found['tagged'], found['amounts'] = tagged, plain
    if found['recent']:
        rest = RECENT.sub(' ', rest)
    found['rest'] = rest
    return found


# ---------------------------------------------------------------- who: a live stay
def stay_place(stay):
    """Where a stay is, with the flat in it: "Ahli house A · 101 · A · A3"."""
    if stay.bed_id:
        return lookup.bed_label(stay.bed)
    if stay.room_id:
        return lookup.room_label(stay.room)
    return stay.location_label()


def stay_choice_label(stay):
    return f'{stay.guest.full_name} · {stay_place(stay)}'


def take_stays(ctx, slots, found):
    """Fill ``tenancy`` (or the options for "which one?") from a message."""
    hits = lookup.find_stays(ctx, found['rest'], found.get('mobile', ''))
    slots.pop('tenancy_options', None)
    if len(hits) == 1:
        slots['tenancy'] = hits[0].pk
    elif hits:
        slots['tenancy_options'] = [s.pk for s in hits[:8]]
    words = lookup.name_words(found['rest'])
    if words:
        slots['who'] = ' '.join(words)
    return bool(hits)


def ask_stay(ctx, slots, question):
    """The question about who, or ``None`` when we know."""
    if slots.get('tenancy') and lookup.stay(ctx, slots['tenancy']):
        return None
    slots.pop('tenancy', None)
    options = slots.get('tenancy_options')
    if options:
        stays = list(lookup.live_stays(ctx).filter(pk__in=options))
        if stays:
            return 'tenancy', say(
                _('More than one guest matches. Which one?'),
                buttons=[choose(stay_choice_label(s), 'tenancy', s.pk) for s in stays]
                + [cancel_button()])
    if slots.get('who'):
        return 'tenancy', say(
            _('I could not find anyone staying called "%(name)s". Type the name again, or '
              'their mobile number.') % {'name': slots['who']}, buttons=[cancel_button()])
    return 'tenancy', say(question, buttons=[cancel_button()])


def answer_stay(ctx, slots, text):
    found = read(ctx, text)
    if take_stays(ctx, slots, found):
        return None
    words = lookup.name_words(text)
    slots['who'] = ' '.join(words) if words else text.strip()[:60]
    return None


def pick_stay(ctx, slots, value):
    if str(value).isdigit() and lookup.stay(ctx, int(value)):
        slots['tenancy'] = int(value)
        slots.pop('tenancy_options', None)


# ---------------------------------------------------------------- who: any guest
def take_guests(ctx, slots, found):
    hits = lookup.find_guests(ctx, found['rest'], found.get('mobile', ''))
    if found.get('recent'):
        # "the recently added guest (Akash)": the newest one wins.
        # With no name at all, the newest guest not yet staying anywhere.
        hits = sorted(hits, key=lambda g: g.created_at, reverse=True)[:1]
        if not hits and not lookup.name_words(found['rest']):
            hits = lookup.newest_guests(ctx)[:1]
    slots.pop('guest_options', None)
    if len(hits) == 1:
        slots['guest'] = hits[0].pk
    elif hits:
        slots['guest_options'] = [g.pk for g in hits[:8]]
    words = lookup.name_words(found['rest'])
    if words:
        slots['who'] = ' '.join(words)
    return bool(hits)


def guest_choice_label(ctx, guest):
    stay = lookup.stay_of(ctx, guest)
    where = stay_place(stay) if stay else ''
    return f'{guest.full_name} · {where or guest.mobile}'


def ask_guest(ctx, slots, question, *, offer_new=False):
    if slots.get('guest') and lookup.guest(ctx, slots['guest']):
        return None
    slots.pop('guest', None)
    options = slots.get('guest_options')
    if options:
        from guests.models import Guest

        guests = list(Guest.objects.filter(tenant=ctx.tenant, pk__in=options).order_by('full_name'))
        if guests:
            return 'guest', say(
                _('More than one guest matches. Which one?'),
                buttons=[choose(guest_choice_label(ctx, g), 'guest', g.pk) for g in guests]
                + [cancel_button()])
    if slots.get('who'):
        from .base import start

        buttons = []
        if offer_new:
            buttons.append(start(_('Add "%(name)s" as a new guest') % {'name': slots['who'].title()},
                                 'add_guest', full_name=slots['who'].title()))
        return 'guest', say(
            _('I could not find a guest called "%(name)s". Type the name again, or their '
              'mobile number.') % {'name': slots['who']}, buttons=buttons + [cancel_button()])
    return 'guest', say(question, buttons=[cancel_button()])


def answer_guest(ctx, slots, text):
    found = read(ctx, text)
    if take_guests(ctx, slots, found):
        return None
    words = lookup.name_words(text)
    slots['who'] = ' '.join(words) if words else text.strip()[:60]
    return None


def pick_guest(ctx, slots, value):
    if str(value).isdigit() and lookup.guest(ctx, int(value)):
        slots['guest'] = int(value)
        slots.pop('guest_options', None)
