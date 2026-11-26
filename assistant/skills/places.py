"""Finding a bed from what was typed: "101 A A2", "room 102", "117 a room a5 bed".

Shared by check-in and bed transfer: the codes are matched against the beds
this user can see, "which one?" is asked when several fit, and a bed that is
taken is refused with the free ones in the same room offered instead.
"""
from django.utils.translation import gettext as _

from guests.models import Tenancy

from .. import lookup
from .base import cancel_button, choose, say


def taken_by(bed):
    """Who is staying in this bed now, or ``''``."""
    stay = Tenancy.objects.filter(bed=bed, status__code__in=Tenancy.LIVE_STATUS_CODES).select_related(
        'guest').first()
    return stay.guest.full_name if stay else ''


class BedPicker:
    """Mixed into a skill that needs a bed (or a room let whole) in its slots."""

    def _occupant(self, ctx, slots, bed):
        """Why ``bed`` cannot be taken: the name of whoever is in it, ``True``
        when it is marked occupied, or ``''`` when it is free."""
        return taken_by(bed) or bool(bed.is_occupied)

    def _taken_note(self, where, who):
        return (_('%(where)s is taken by %(name)s.') if who else
                _('%(where)s is not free.')) % {'where': where, 'name': who}

    def _take_place(self, ctx, slots, found):
        """Which bed (or room let whole) the codes typed point to, checked
        against what this user can see."""
        building = found.get('building')
        if building is not None:
            slots['building'] = building.pk
        codes = found.get('codes') or []
        if not codes:
            found['unused'] = []
            return False
        if building is None and slots.get('building'):
            building = lookup.building(ctx, slots['building'])
        places, unused = lookup.match_places(ctx, codes, building)
        found['unused'] = unused
        place_codes = [c for c in unused if not c.isdigit() or any(
            kind for code, kind in codes if code == c)]
        for key in ('bed', 'room', 'place_options', 'place_note'):
            slots.pop(key, None)
        if not places:
            slots['place_note'] = _('I could not find %(what)s.') % {
                'what': ' / '.join(code.upper() for code, _kind in codes)}
            return True
        if place_codes:
            slots['place_note'] = _('I could not find %(what)s, so I looked for the rest.') % {
                'what': ' / '.join(code.upper() for code in place_codes)}
        if len(places) == 1:
            self._set_place(slots, *places[0])
        else:
            slots['place_options'] = [f'{kind[0]}{obj.pk}' for kind, obj in places[:40]]
        return True

    def _set_place(self, slots, kind, obj):
        if kind == 'bed':
            slots['bed'], slots['room'] = obj.pk, obj.room_id
        else:
            slots.pop('bed', None)
            slots['room'] = obj.pk
        slots.pop('place_options', None)

    def _free_bed_buttons(self, ctx, slots, room=None):
        building = lookup.building(ctx, slots['building']) if slots.get('building') else None
        beds = list(lookup.free_beds(ctx, building=building, room=room)[:6])
        return [choose(lookup.bed_label(b) if room is None else _('Bed %(bed)s') % {'bed': b.label},
                       'place', f'b{b.pk}') for b in beds]

    def _ask_place(self, ctx, slots):
        if slots.get('place_options'):
            options = []
            for token in slots['place_options']:
                kind = 'bed' if token[0] == 'b' else 'room'
                obj = lookup.bed(ctx, int(token[1:])) if kind == 'bed' else lookup.room(
                    ctx, int(token[1:]))
                if obj is not None and not (kind == 'bed' and self._occupant(ctx, slots, obj)):
                    options.append((kind, obj))
            note = slots.get('place_note', '')
            if len(options) == 1:
                self._set_place(slots, *options[0])
            elif options:
                return 'place', say(' '.join(filter(None, [note, _(
                    'More than one free bed fits. Which one?')])),
                    buttons=[choose(lookup.place_label(k, o), 'place', f'{k[0]}{o.pk}')
                             for k, o in options[:8]] + [cancel_button()])
            else:
                slots.pop('place_options', None)
                slots['place_note'] = ' '.join(filter(None, [note, _(
                    'Every bed that fits is taken.')]))
        room = lookup.room(ctx, slots['room']) if slots.get('room') else None
        bed = lookup.bed(ctx, slots['bed']) if slots.get('bed') else None
        occupant = self._occupant(ctx, slots, bed) if bed is not None else None
        if occupant:
            who = occupant if isinstance(occupant, str) else ''
            slots.pop('bed')
            where = lookup.bed_label(bed)
            note = self._taken_note(where, who)
            buttons = self._free_bed_buttons(ctx, slots, room=bed.room)
            text = ' '.join(filter(None, [slots.pop('place_note', ''), note, (
                _('Pick a free bed in the same room:') if buttons else
                _('Type another bed, like "101 A A2".'))]))
            return 'place', say(text, buttons=buttons + [cancel_button()])
        if bed is not None or (room is not None and room.lets_whole_unit):
            return None          # any note ("could not find 999") shows in the summary
        note = slots.pop('place_note', '')
        if room is not None:
            buttons = self._free_bed_buttons(ctx, slots, room=room)
            text = _('Which bed in room %(room)s?') % {'room': room.number}
            if not buttons:
                text = _('Room %(room)s has no free bed. Type another room and bed.') % {
                    'room': room.number}
            return 'place', say(' '.join(filter(None, [note, text])),
                                buttons=buttons + [cancel_button()])
        buttons = self._free_bed_buttons(ctx, slots)
        text = _('Which bed? Type it like "apartment 101 room A bed A2"') + (
            _(', or pick a free one:') if buttons else '.')
        return 'place', say(' '.join(filter(None, [note, text])), buttons=buttons + [cancel_button()])

    def pick_place(self, ctx, slots, key, value):
        """A button for a bed or room: ``place=b12``, ``bed=12`` or ``room=7``."""
        value = str(value)
        if key != 'place':
            value = key[0] + value
        if len(value) > 1 and value[1:].isdigit():
            obj = (lookup.bed(ctx, int(value[1:])) if value[0] == 'b'
                   else lookup.room(ctx, int(value[1:])))
            if obj is not None:
                self._set_place(slots, 'bed' if value[0] == 'b' else 'room', obj)
                slots.pop('place_note', None)
