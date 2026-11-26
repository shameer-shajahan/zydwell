"""Check a guest in: "put Ravi in room 101 bed A at 800, deposit 500 paid"."""
import re
from datetime import date, timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils.translation import gettext as _

from core.constants import MODULE_BILLING, MODULE_GUESTS
from guests import services
from guests.forms import CheckInForm
from guests.models import Tenancy

from .. import lookup
from ..brain import parse
from .add_guest import form_errors
from .base import (Handoff, NotDone, Skill, as_date, as_decimal, cancel_button, choose,
                   confirm_buttons, day, link, say, start)
from .common import answer_guest, ask_guest, pick_guest, read, stay_place, take_guests
from .places import BedPicker


TAGS = ('deposit_received', 'deposit', 'rent')


def to_collect_note(money, tenancy, invoice):
    """Everything still to collect from a new stay: the first rent and the deposit."""
    deposit_due = max(tenancy.deposit_amount - tenancy.deposit_held(), Decimal('0'))
    rent_due = invoice.balance if invoice is not None else Decimal('0')
    return _('To collect: %(rent)s rent + %(deposit)s deposit = %(total)s.') % {
        'rent': money(rent_due), 'deposit': money(deposit_due),
        'total': money(rent_due + deposit_due)}


class CheckIn(BedPicker, Skill):
    name = 'check_in'
    module = MODULE_GUESTS
    action = 'create'
    title = _('Check in to a bed')
    example = _('put Ravi in apartment 101 room A bed A2 at 800, deposit 500 paid')

    # ------------------------------------------------------------ reading
    def start(self, ctx, text):
        slots = {}
        found = read(ctx, text, tags=TAGS, forward=None, place='codes')
        take_guests(ctx, slots, found)
        staying = slots.get('guest') and lookup.stay_of(ctx, lookup.guest(ctx, slots['guest']))
        if staying and found.get('date') and not found.get('codes'):
            # Already staying, a date and no bed: the check-in date is being corrected.
            raise Handoff('edit_check_in')
        if staying and found.get('codes') and not found.get('recent'):
            # Already staying and a bed named: "move him to 102 a1" is a transfer.
            raise Handoff('transfer')
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
            self.pick(ctx, slots, 'place', f"b{values['bed']}")
        if str(values.get('room', '')).isdigit():
            self.pick(ctx, slots, 'place', f"r{values['room']}")
        return slots

    def _take_money(self, slots, found, plain_is_rent=False):
        tagged = found['tagged']
        # Bare numbers: those no bed took, or — when no bed was looked for — all.
        loose = found['unused'] if 'unused' in found else [
            code for code, _kind in found.get('codes', [])]
        plain = list(found['amounts']) + [Decimal(code) for code in loose if code.isdigit()]
        changed = False
        if 'rent' in tagged:
            slots['rent'] = str(tagged['rent'])
            changed = True
        elif plain_is_rent and plain:
            # No monthly rent is 31 or less: such a number is a day of the month.
            small = [n for n in plain if n <= 31]
            rents = [n for n in plain if n > 31]
            if rents:
                slots['rent'] = str(rents[0])
                changed = True
            if small and not found.get('date'):
                when = parse.day_of_month(int(small[0]), found['today'])
                if when:
                    slots['date'] = when.isoformat()
                    changed = True
        if 'deposit_received' in tagged:
            slots['deposit_received'] = str(tagged['deposit_received'])
            slots.setdefault('deposit', str(tagged['deposit_received']))
            changed = True
        if 'deposit' in tagged:
            slots['deposit'] = str(tagged['deposit'])
            changed = True
        return changed

    # ------------------------------------------------------------ asking
    def ask(self, ctx, slots):
        question = ask_guest(ctx, slots, _('Who is checking in? Type the guest\'s name.'),
                             offer_new=True)
        if question:
            return question
        return (self._ask_place(ctx, slots) or self._ask_rent(ctx, slots)
                or self._ask_date(ctx, slots) or self._ask_deposit(ctx, slots))

    def _ask_date(self, ctx, slots):
        """The check-in day, typed or picked from a calendar — never assumed."""
        if slots.get('date'):
            return None
        guest = lookup.guest(ctx, slots['guest'])
        today, yesterday = ctx.today, ctx.today - timedelta(days=1)
        return 'date', say(
            _('Which day does %(name)s check in? Tap a button, pick a date or type it.') % {
                'name': guest.full_name},
            buttons=[choose(_('Today, %(date)s') % {'date': day(today)}, 'date', today.isoformat()),
                     choose(_('Yesterday'), 'date', yesterday.isoformat()), cancel_button()],
            date=('date', today.isoformat()))

    def _ask_deposit(self, ctx, slots):
        """Whether a deposit was paid at check-in, and how much."""
        if slots.get('deposit_asked'):
            return None
        guest = lookup.guest(ctx, slots['guest'])
        if 'deposit' not in slots:
            return 'deposit', say(
                _('Did %(name)s pay a deposit? Type the amount paid now, like 500, or '
                  '"500 agreed, not paid".') % {'name': guest.full_name},
                buttons=[choose(_('No deposit'), 'deposit', 'none'), cancel_button()])
        if 'deposit_received' not in slots and as_decimal(slots['deposit']):
            amount = ctx.money(slots['deposit'])
            return 'deposit', say(
                _('Was the %(amount)s deposit received now?') % {'amount': amount},
                buttons=[choose(_('Yes, received %(amount)s') % {'amount': amount}, 'deposit',
                                'paid'),
                         choose(_('Not yet'), 'deposit', 'later'), cancel_button()])
        return None

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
        found = read(ctx, text, tags=TAGS, forward=None, place='codes')
        if key == 'place':
            placed = self._take_place(ctx, slots, found)
            # "date 1" or "rent 700" while the bed is still open: keep them.
            other = self._take_money(slots, found, plain_is_rent=True)
            if found.get('date'):
                slots['date'] = found['date'].isoformat()
                other = True
            if not placed and not other:
                return say(_('Type the apartment, room and bed, like "101 A A2".'),
                           buttons=[cancel_button()])
            return None
        if key == 'date':
            if not found.get('date'):
                return say(_('Pick the date, or type it like "1 Dec" or "25/11".'),
                           buttons=[cancel_button()], date=('date', ctx.today.isoformat()))
            slots['date'] = found['date'].isoformat()
            return None
        if key == 'deposit':
            return self._answer_deposit(ctx, slots, found)
        if key == 'rent':
            took = self._take_money(slots, found, plain_is_rent=True)
            if found.get('date'):
                # "date 20" while the rent is asked: keep it, then ask the rent again.
                slots['date'] = found['date'].isoformat()
                took = True
            if not took:
                return say(_('Type the monthly rent as a number, like 800.'),
                           buttons=[cancel_button()])
        return None

    _NO = re.compile(r'\b(?:no|none|nothing|zero|nil|without)\b', re.I)
    _LATER = re.compile(r'\b(?:not\s+(?:paid|yet|received)|later|pending|agreed|due|owe)\b', re.I)

    def _answer_deposit(self, ctx, slots, found):
        text = found['text']
        numbers = list(found['amounts']) + [Decimal(c) for c, _k in found.get('codes', [])
                                            if c.isdigit()]
        tagged = found['tagged']
        if 'deposit_received' in tagged or 'deposit' in tagged:
            self._take_money(slots, found)
        elif numbers:
            agreed = max(numbers)
            slots['deposit'] = str(agreed)
            if self._LATER.search(text):
                slots['deposit_received'] = '0'
            else:
                slots['deposit_received'] = str(min(numbers) if len(numbers) > 1 else agreed)
        elif self._NO.search(text):
            slots['deposit'] = slots['deposit_received'] = '0'
        elif self._LATER.search(text) and as_decimal(slots.get('deposit')):
            slots['deposit_received'] = '0'
        elif re.search(r'\b(?:yes|received|paid|got|took)\b', text, re.I) and as_decimal(
                slots.get('deposit')):
            slots['deposit_received'] = slots['deposit']
        else:
            return say(_('Type the deposit paid now, like 500, or tap "No deposit".'),
                       buttons=[choose(_('No deposit'), 'deposit', 'none'), cancel_button()])
        slots['deposit_asked'] = True
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'date':
            try:
                slots['date'] = date.fromisoformat(str(value)).isoformat()
            except ValueError:
                pass
            return
        if key == 'deposit':
            if value == 'none':
                slots['deposit'] = slots['deposit_received'] = '0'
            elif value == 'paid':
                slots['deposit_received'] = slots.get('deposit', '0')
            elif value == 'later':
                slots['deposit_received'] = '0'
            slots['deposit_asked'] = True
            return
        if key == 'guest':
            pick_guest(ctx, slots, value)
        elif key in ('place', 'bed', 'room'):
            self.pick_place(ctx, slots, key, value)

    def change(self, ctx, slots, text):
        found = read(ctx, text, tags=TAGS, forward=None, place='codes')
        changed = False
        if found['codes'] and any(kind for _code, kind in found['codes']):
            # "bed A3" or "room 102": a new place. Bare numbers stay money or dates.
            self._take_place(ctx, slots, found)
            changed = True
        else:
            found['unused'] = [code for code, _kind in found['codes']]
        if found.get('date'):
            slots['date'] = found['date'].isoformat()
            changed = True
        return self._take_money(slots, found, plain_is_rent=True) or changed

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
        ]
        bill_day = services.bill_day_for(when)
        period_start, period_end = Tenancy(
            billing_cycle=Tenancy.CYCLE_MONTHLY, billing_day=bill_day).period_for(when)
        rows += [
            (_('Bill day'), _('day %(day)s of each month') % {'day': bill_day}),
            (_('First invoice'), _('Full rent for %(start)s – %(end)s, raised now') % {
                'start': day(max(period_start, when)), 'end': day(period_end)}),
        ]
        rent = as_decimal(slots['rent']) or Decimal('0')
        deposit_due = max((as_decimal(slots.get('deposit')) or Decimal('0'))
                          - (as_decimal(slots.get('deposit_received')) or Decimal('0')),
                          Decimal('0'))
        rows.append((_('To collect'), _('%(rent)s rent + %(deposit)s deposit = %(total)s') % {
            'rent': ctx.money(rent), 'deposit': ctx.money(deposit_due),
            'total': ctx.money(rent + deposit_due)}))
        current = lookup.stay_of(ctx, guest)
        text = _('Please check the check-in. To change something, type it, for example '
                 '"rent 900", "date 1" or "deposit 500 paid".')
        if slots.get('place_note'):
            text = slots['place_note'] + ' ' + text
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
            'billing_day': services.bill_day_for(when), 'rent_mode': Tenancy.MODE_ADVANCE,
            'first_charge': 'full',
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
        from .common import remember
        remember(ctx, tenancy=tenancy)
        text = _('Done! %(name)s is checked in to %(where)s.') % {
            'name': tenancy.guest.full_name, 'where': stay_place(tenancy)}
        text += ' ' + _('Invoice %(no)s raised for %(amount)s.') % {
            'no': invoice.number, 'amount': ctx.money(invoice.total)}
        text += ' ' + to_collect_note(ctx.money, tenancy, invoice)
        buttons = [link(_('Open stay'), reverse('guests:tenancy_detail', args=[tenancy.pk]))]
        if ctx.allowed(MODULE_BILLING, 'create') and invoice.balance > 0:
            buttons.append(start(_('Collect rent now'), 'payment', tenancy=tenancy.pk,
                                 amount=invoice.balance))
        return [say(text, buttons=buttons)]
