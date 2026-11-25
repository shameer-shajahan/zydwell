"""Check a guest in: "put Ravi in room 101 bed A at 800, deposit 500 paid"."""
from decimal import Decimal

from django.urls import reverse
from django.utils.translation import gettext as _

from core.constants import MODULE_BILLING, MODULE_GUESTS
from guests import services
from guests.forms import CheckInForm
from guests.models import Tenancy

from .. import lookup
from .add_guest import form_errors
from .base import (NotDone, Skill, as_date, as_decimal, cancel_button, choose, confirm_buttons,
                   day, link, say, start)
from .common import answer_guest, ask_guest, pick_guest, read, take_guests

TAGS = ('deposit_received', 'deposit', 'rent')


def _taken_by(bed):
    stay = Tenancy.objects.filter(bed=bed, status__code__in=Tenancy.LIVE_STATUS_CODES).select_related(
        'guest').first()
    return stay.guest.full_name if stay else ''


class CheckIn(Skill):
    name = 'check_in'
    module = MODULE_GUESTS
    action = 'create'
    example = _('put Ravi in room 101 bed A at 800, deposit 500 paid')

    # ------------------------------------------------------------ reading
    def start(self, ctx, text):
        slots = {}
        found = read(ctx, text, tags=TAGS, forward=True, place=True)
        take_guests(ctx, slots, found)
        self._take_place(ctx, slots, found)
        self._take_money(slots, found, plain_is_rent=True)
        if found.get('date'):
            slots['date'] = found['date'].isoformat()
        return slots

    def preset(self, ctx, values):
        slots = {}
        if str(values.get('guest', '')).isdigit():
            pick_guest(ctx, slots, values['guest'])
        if str(values.get('bed', '')).isdigit():
            self.pick(ctx, slots, 'bed', values['bed'])
        if str(values.get('room', '')).isdigit():
            self.pick(ctx, slots, 'room', values['room'])
        return slots

    def _take_money(self, slots, found, plain_is_rent=False):
        tagged, plain = found['tagged'], list(found['amounts'])
        changed = False
        if 'rent' in tagged:
            slots['rent'] = str(tagged['rent'])
            changed = True
        elif plain_is_rent and plain:
            slots['rent'] = str(plain.pop(0))
            changed = True
        if 'deposit_received' in tagged:
            slots['deposit_received'] = str(tagged['deposit_received'])
            slots.setdefault('deposit', str(tagged['deposit_received']))
            changed = True
        if 'deposit' in tagged:
            slots['deposit'] = str(tagged['deposit'])
            changed = True
        return changed

    def _take_place(self, ctx, slots, found):
        """Room, bed or flat from the message, checked against what this user sees."""
        building = found.get('building')
        if building is not None:
            slots['building'] = building.pk
        room_no, label, flat = found.get('room'), found.get('bed'), found.get('flat', '')
        if not (room_no or label or flat):
            return False
        for key in ('bed', 'room', 'room_options', 'place_note'):
            slots.pop(key, None)
        if room_no or flat:
            rooms = lookup.find_rooms(ctx, room_no, building, flat) if room_no else [
                r for r in lookup.rooms(ctx).filter(apartment__name__iexact=flat)
                if r.lets_whole_unit]
            if not rooms:
                slots['place_note'] = _('I could not find room %(room)s.') % {
                    'room': room_no or flat}
                return True
            if len(rooms) > 1:
                slots['room_options'] = [r.pk for r in rooms[:8]]
                slots['bed_label'] = label or ''
                return True
            room = rooms[0]
            slots['room'] = room.pk
            if label:
                beds = lookup.find_beds(ctx, label, room=room)
                if beds:
                    slots['bed'] = beds[0].pk
                else:
                    slots['place_note'] = _('Room %(room)s has no bed %(bed)s.') % {
                        'room': room.number, 'bed': label}
            return True
        beds = lookup.find_beds(ctx, label, building=building)
        if len(beds) == 1:
            slots['bed'], slots['room'] = beds[0].pk, beds[0].room_id
        else:
            slots['place_note'] = _('Which room is bed %(bed)s in?') % {'bed': label}
        return True

    # ------------------------------------------------------------ asking
    def ask(self, ctx, slots):
        question = ask_guest(ctx, slots, _('Who is checking in? Type the guest\'s name.'),
                             offer_new=True)
        if question:
            return question
        return self._ask_place(ctx, slots) or self._ask_rent(ctx, slots)

    def _free_bed_buttons(self, ctx, slots, room=None):
        building = lookup.building(ctx, slots['building']) if slots.get('building') else None
        beds = list(lookup.free_beds(ctx, building=building, room=room)[:6])
        return [choose(lookup.bed_label(b) if room is None else _('Bed %(bed)s') % {'bed': b.label},
                       'bed', b.pk) for b in beds]

    def _ask_place(self, ctx, slots):
        if slots.get('room_options'):
            rooms = list(lookup.rooms(ctx).filter(pk__in=slots['room_options']))
            return 'place', say(_('There is more than one room with that number. Which one?'),
                                buttons=[choose(lookup.room_label(r), 'room', r.pk) for r in rooms]
                                + [cancel_button()])
        room = lookup.room(ctx, slots['room']) if slots.get('room') else None
        bed = lookup.bed(ctx, slots['bed']) if slots.get('bed') else None
        if bed is not None and (bed.is_occupied or _taken_by(bed)):
            who = _taken_by(bed)
            slots.pop('bed')
            note = (_('Bed %(bed)s in room %(room)s is taken by %(name)s.') if who else
                    _('Bed %(bed)s in room %(room)s is not free.')) % {
                'bed': bed.label, 'room': bed.room.number, 'name': who}
            buttons = self._free_bed_buttons(ctx, slots, room=bed.room)
            return 'place', say(note + ' ' + (_('Pick a free bed:') if buttons else
                                              _('Type another bed, like "room 102 bed A".')),
                                buttons=buttons + [cancel_button()])
        if bed is not None:
            return None
        if room is not None and room.lets_whole_unit:
            return None
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
        text = _('Which bed? Type it like "room 101 bed A"') + (
            _(', or pick a free one:') if buttons else '.')
        return 'place', say(' '.join(filter(None, [note, text])), buttons=buttons + [cancel_button()])

    def _ask_rent(self, ctx, slots):
        if as_decimal(slots.get('rent')):
            return None
        bed = lookup.bed(ctx, slots['bed']) if slots.get('bed') else None
        room = lookup.room(ctx, slots['room']) if slots.get('room') else None
        default = bed.monthly_price if bed is not None else (room.monthly_rent if room else None)
        if default and default > 0:
            slots['rent'] = str(default)
            return None
        return 'rent', say(_('What is the monthly rent?'), buttons=[cancel_button()])

    # ------------------------------------------------------------ replies
    def answer(self, ctx, slots, key, text):
        if key == 'guest':
            return answer_guest(ctx, slots, text)
        found = read(ctx, text, tags=TAGS, place=True)
        if key == 'place':
            if not self._take_place(ctx, slots, found):
                return say(_('Type the room and bed, like "room 101 bed A".'),
                           buttons=[cancel_button()])
            return None
        if key == 'rent':
            if not self._take_money(slots, found, plain_is_rent=True):
                return say(_('Type the monthly rent as a number, like 800.'),
                           buttons=[cancel_button()])
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'guest':
            pick_guest(ctx, slots, value)
        elif key == 'bed' and str(value).isdigit():
            bed = lookup.bed(ctx, int(value))
            if bed is not None:
                slots['bed'], slots['room'] = bed.pk, bed.room_id
                slots.pop('room_options', None)
        elif key == 'room' and str(value).isdigit():
            room = lookup.room(ctx, int(value))
            if room is not None:
                slots['room'] = room.pk
                slots.pop('room_options', None)
                label = slots.pop('bed_label', '')
                if label:
                    beds = lookup.find_beds(ctx, label, room=room)
                    if beds:
                        slots['bed'] = beds[0].pk

    def change(self, ctx, slots, text):
        found = read(ctx, text, tags=TAGS, place=True)
        changed = self._take_money(slots, found, plain_is_rent=True)
        if found.get('date'):
            slots['date'] = found['date'].isoformat()
            changed = True
        if found.get('room') or found.get('bed') or found.get('flat'):
            self._take_place(ctx, slots, found)
            changed = True
        return changed

    # ------------------------------------------------------------ saving
    def summary(self, ctx, slots):
        guest = lookup.guest(ctx, slots['guest'])
        bed = lookup.bed(ctx, slots['bed']) if slots.get('bed') else None
        room = lookup.room(ctx, slots['room'])
        when = as_date(slots.get('date')) or ctx.today
        rows = [
            (_('Guest'), guest.full_name),
            (_('Bed') if bed else _('Room'), lookup.bed_label(bed) if bed else lookup.room_label(room)),
            (_('Check-in date'), day(when)),
            (_('Monthly rent'), ctx.money(slots['rent'])),
            (_('Deposit agreed'), ctx.money(slots.get('deposit') or 0)),
            (_('Deposit received now'), ctx.money(slots.get('deposit_received') or 0)),
            (_('First invoice'), _('Full rent for the first period, raised now')),
        ]
        current = lookup.stay_of(ctx, guest)
        text = _('Please check the check-in. To change something, type it, for example '
                 '"rent 900" or "deposit 500 paid".')
        if current:
            text = _('Note: %(name)s already stays at %(where)s. ') % {
                'name': guest.full_name, 'where': current.location_label()} + text
        return say(text, rows=rows, buttons=confirm_buttons(_('Yes, check in')))

    def run(self, ctx, slots):
        when = as_date(slots.get('date')) or ctx.today
        form = CheckInForm(data={
            'guest': slots['guest'], 'room': slots['room'], 'bed': slots.get('bed') or '',
            'check_in_date': when.isoformat(), 'expected_check_out': '',
            'monthly_rent': slots['rent'], 'billing_cycle': Tenancy.CYCLE_MONTHLY,
            'billing_day': 1, 'rent_mode': Tenancy.MODE_ADVANCE, 'first_charge': 'full',
            'first_amount': '', 'deposit_amount': slots.get('deposit') or '0',
            'deposit_received': slots.get('deposit_received') or '0',
            'raise_first_invoice': 'on', 'agreement_note': '',
        }, tenant=ctx.tenant, user=ctx.user)
        if not form.is_valid():
            raise NotDone([say(_('I could not check in: %(problems)s Type a correction or say '
                                 '"cancel".') % {'problems': form_errors(form)})])
        data = form.cleaned_data
        prorate, first_amount = form.billing_choices()
        tenancy = services.check_in(
            tenant=ctx.tenant, user=ctx.user, guest=data['guest'], room=data['room'],
            bed=data['bed'], check_in_date=data['check_in_date'],
            monthly_rent=data['monthly_rent'], deposit_amount=data['deposit_amount'],
            deposit_received=data['deposit_received'] or Decimal('0'),
            billing_day=data['billing_day'], billing_cycle=data['billing_cycle'],
            expected_check_out=data['expected_check_out'], agreement_note=data['agreement_note'],
            rent_mode=data['rent_mode'], prorate_first_period=prorate,
            first_invoice_amount=first_amount,
        )
        invoice = services.raise_first_invoice(tenancy=tenancy, user=ctx.user)
        text = _('Done! %(name)s is checked in to %(where)s.') % {
            'name': tenancy.guest.full_name, 'where': tenancy.unit_label() or tenancy.location_label()}
        text += ' ' + _('Invoice %(no)s raised for %(amount)s.') % {
            'no': invoice.number, 'amount': ctx.money(invoice.total)}
        buttons = [link(_('Open stay'), reverse('guests:tenancy_detail', args=[tenancy.pk]))]
        if ctx.allowed(MODULE_BILLING, 'create') and invoice.balance > 0:
            buttons.append(start(_('Collect rent now'), 'payment', tenancy=tenancy.pk,
                                 amount=invoice.balance))
        return [say(text, buttons=buttons)]
