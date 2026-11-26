"""Move a guest to another bed: "move Ravi to 101 A A3", "shift him to room 102 from 1 Dec"."""
from django.urls import reverse
from django.utils.translation import gettext as _

from core.constants import MODULE_GUESTS
from guests import services
from guests.forms import TransferForm
from properties.models import Room

from .. import lookup
from .add_guest import form_errors
from .base import (Finish, Handoff, NotDone, Skill, as_date, as_decimal, cancel_button, confirm_buttons,
                   day, link, say)
from .common import answer_stay, ask_stay, pick_stay, read, stay_place, take_stays
from .places import BedPicker

TAGS = ('rent',)


class Transfer(BedPicker, Skill):
    name = 'transfer'
    module = MODULE_GUESTS
    action = 'edit'
    title = _('Move to another bed')
    example = _('move Ravi to 101 A A3 from 1 Dec')

    def start(self, ctx, text):
        slots = {}
        found = read(ctx, text, tags=TAGS, forward=None, place='codes')
        take_stays(ctx, slots, found)
        if not (slots.get('tenancy') or slots.get('tenancy_options')) and lookup.find_guests(
                ctx, found['rest'], found.get('mobile', '')):
            raise Handoff('check_in')         # not staying anywhere: a check-in
        self._take(ctx, slots, found)
        return slots

    def preset(self, ctx, values):
        slots = {}
        if str(values.get('tenancy', '')).isdigit():
            pick_stay(ctx, slots, values['tenancy'])
        return slots

    def _take(self, ctx, slots, found):
        changed = self._take_place(ctx, slots, found)
        if 'rent' in found['tagged']:
            slots['rent'] = str(found['tagged']['rent'])
            changed = True
        if found.get('date'):
            slots['date'] = found['date'].isoformat()
            changed = True
        return changed

    def _occupant(self, ctx, slots, bed):
        stay = lookup.stay(ctx, slots['tenancy']) if slots.get('tenancy') else None
        if stay is not None and stay.bed_id == bed.pk:
            return _('this guest already')
        return super()._occupant(ctx, slots, bed)

    def _taken_note(self, where, who):
        if who == _('this guest already'):
            return _('The guest is already in %(where)s.') % {'where': where}
        return super()._taken_note(where, who)

    def ask(self, ctx, slots):
        question = ask_stay(ctx, slots, _('Who is moving? Type the guest\'s name.'))
        if question:
            return question
        stay = lookup.stay(ctx, slots['tenancy'])
        if stay.is_team_let:
            raise Finish([say(_('%(name)s is a team let. Move it on the team page.') % {
                'name': stay.guest.full_name},
                buttons=[link(_('Open team'), reverse('teams:team_edit', args=[stay.pk]))])])
        return self._ask_place(ctx, slots)

    def answer(self, ctx, slots, key, text):
        if key == 'tenancy':
            return answer_stay(ctx, slots, text)
        if not self._take(ctx, slots, read(ctx, text, tags=TAGS, forward=None, place='codes')):
            return say(_('Type the new bed, like "101 A A3".'), buttons=[cancel_button()])
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'tenancy':
            pick_stay(ctx, slots, value)
        elif key in ('place', 'bed', 'room'):
            self.pick_place(ctx, slots, key, value)

    def change(self, ctx, slots, text):
        found = read(ctx, text, tags=TAGS, forward=None, place='codes')
        if not any(kind for _code, kind in found['codes']):
            found['codes'] = []          # a bare number is the rent or a date, not a bed
        return self._take(ctx, slots, found)

    def _new_rent(self, ctx, slots, stay):
        """The rent after the move: what was typed, else the new bed's or room's price."""
        typed = as_decimal(slots.get('rent'))
        if typed:
            return typed
        bed = lookup.bed(ctx, slots['bed']) if slots.get('bed') else None
        room = lookup.room(ctx, slots['room'])
        if bed is not None and room.rent_basis == Room.RENT_PER_BED and bed.monthly_price:
            return bed.monthly_price
        return room.monthly_rent or stay.monthly_rent

    def summary(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        bed = lookup.bed(ctx, slots['bed']) if slots.get('bed') else None
        room = lookup.room(ctx, slots['room'])
        when = as_date(slots.get('date')) or ctx.today
        rows = [(_('Guest'), stay.guest.full_name),
                (_('From'), stay_place(stay)),
                (_('To'), lookup.bed_label(bed) if bed else lookup.room_label(room)),
                (_('Moving on'), day(when)),
                (_('Rent'), f'{ctx.money(stay.monthly_rent)} → {ctx.money(self._new_rent(ctx, slots, stay))}')]
        text = _('Move the guest? The rent for this period is split by the days in each bed. '
                 'To change something, type it, for example "rent 700" or "from 1 Dec".')
        if slots.get('place_note'):
            text = slots['place_note'] + ' ' + text
        return say(text, rows=rows, buttons=confirm_buttons(_('Yes, move')))

    def run(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        when = as_date(slots.get('date')) or ctx.today
        form = TransferForm(data={
            'new_room': slots['room'], 'new_bed': slots.get('bed') or '',
            'transfer_date': when.isoformat(), 'new_rent': slots.get('rent') or '',
            'reason': '',
        }, tenancy=stay, user=ctx.user)
        if not form.is_valid():
            raise NotDone([say(_('I could not move the guest: %(problems)s Type a correction or '
                                 'say "cancel".') % {'problems': form_errors(form)})])
        data = form.cleaned_data
        record = services.transfer_bed(
            tenancy=stay, user=ctx.user, new_bed=data['new_bed'], new_room=data['new_room'],
            transfer_date=data['transfer_date'], new_rent=data['new_rent'],
            reason=data['reason'])
        stay.refresh_from_db()
        return [say(_('Done! %(name)s is now in %(where)s. For this period: %(old)s on the old '
                      'bed, %(new)s on the new one.') % {
            'name': stay.guest.full_name, 'where': stay_place(stay),
            'old': ctx.money(record.old_prorata), 'new': ctx.money(record.new_prorata)},
            buttons=[link(_('Open stay'), reverse('guests:tenancy_detail', args=[stay.pk]))])]

