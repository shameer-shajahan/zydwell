"""Take a deposit: "Akash paid deposit 500 cash", "security 1000 received from Ravi by card"."""
from decimal import Decimal

from django.urls import reverse
from django.utils.translation import gettext as _

from billing.models import PaymentMode
from core.constants import MODULE_GUESTS
from guests import services
from guests.forms import DepositForm

from .. import lookup
from .add_guest import form_errors
from .base import (NotDone, Skill, as_decimal, cancel_button, choose, confirm_buttons, link, say)
from .common import answer_stay, ask_stay, pick_stay, read, stay_place, take_stays

TAGS = ('deposit_received', 'deposit')


class Deposit(Skill):
    name = 'deposit'
    module = MODULE_GUESTS
    action = 'edit'
    title = _('Take a deposit')
    example = _('Akash paid deposit 500 cash')

    def start(self, ctx, text):
        slots = {}
        found = read(ctx, text, tags=TAGS, forward=False)
        take_stays(ctx, slots, found)
        self._take(ctx, slots, found)
        return slots

    def preset(self, ctx, values):
        slots = {}
        if str(values.get('tenancy', '')).isdigit():
            pick_stay(ctx, slots, values['tenancy'])
        amount = as_decimal(values.get('amount'))
        if amount and amount > 0:
            slots['amount'] = str(amount)
        return slots

    def _take(self, ctx, slots, found):
        changed = False
        tagged = found['tagged']
        amount = tagged.get('deposit_received') or tagged.get('deposit') or (
            found['amounts'][0] if found['amounts'] else None)
        if amount:
            slots['amount'] = str(amount)
            changed = True
        mode = lookup.find_mode(ctx, found['text'])
        if mode is not None and not mode.settles_from_credit:
            slots['mode'] = mode.pk
            changed = True
        if found.get('reference'):
            slots['reference'] = found['reference']
            changed = True
        return changed

    @staticmethod
    def _missing(stay):
        return max(stay.deposit_amount - stay.deposit_held(), Decimal('0'))

    def ask(self, ctx, slots):
        question = ask_stay(ctx, slots, _('Whose deposit is it? Type the guest\'s name.'))
        if question:
            return question
        stay = lookup.stay(ctx, slots['tenancy'])
        if not as_decimal(slots.get('amount')):
            missing = self._missing(stay)
            buttons = [choose(_('%(amount)s (still to collect)') % {'amount': ctx.money(missing)},
                              'amount', missing)] if missing > 0 else []
            return 'amount', say(
                _('How much deposit did %(name)s give? Agreed %(agreed)s, held %(held)s.') % {
                    'name': stay.guest.full_name, 'agreed': ctx.money(stay.deposit_amount),
                    'held': ctx.money(stay.deposit_held())},
                buttons=buttons + [cancel_button()])
        mode = lookup.mode(ctx, slots['mode']) if slots.get('mode') else None
        if mode is None:
            mode = lookup.default_mode(ctx)
            slots['mode'] = mode.pk if mode else None
        if mode is not None and mode.needs_reference and not slots.get('reference'):
            return 'reference', say(_('What is the %(mode)s reference number?') % {
                'mode': mode.name.lower()}, buttons=[cancel_button()])
        return None

    def answer(self, ctx, slots, key, text):
        if key == 'tenancy':
            return answer_stay(ctx, slots, text)
        found = read(ctx, text, tags=TAGS, forward=False)
        if key == 'amount' and not self._take(ctx, slots, found):
            return say(_('Type the amount as a number, like 500.'), buttons=[cancel_button()])
        if key == 'reference':
            slots['reference'] = (found.get('reference') or text.strip().split()[-1]).upper()[:80]
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'tenancy':
            pick_stay(ctx, slots, value)
        elif key == 'amount':
            amount = as_decimal(value)
            if amount and amount > 0:
                slots['amount'] = str(amount)

    def change(self, ctx, slots, text):
        return self._take(ctx, slots, read(ctx, text, tags=TAGS, forward=False))

    def summary(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        amount = as_decimal(slots['amount'])
        mode = PaymentMode.objects.filter(pk=slots.get('mode')).first()
        held = stay.deposit_held()
        rows = [(_('Guest'), stay.guest.full_name), (_('Bed'), stay_place(stay)),
                (_('Deposit agreed'), ctx.money(stay.deposit_amount)),
                (_('Held now'), ctx.money(held)),
                (_('Receiving'), ctx.money(amount)),
                (_('Paid by'), mode.name if mode else '—')]
        if slots.get('reference'):
            rows.append((_('Reference'), slots['reference']))
        rows.append((_('Held after this'), ctx.money(held + amount)))
        text = _('Record this deposit?')
        if held + amount > stay.deposit_amount:
            text += ' ' + _('That is more than the %(agreed)s agreed.') % {
                'agreed': ctx.money(stay.deposit_amount)}
        return say(text, rows=rows, buttons=confirm_buttons(_('Yes, record deposit')))

    def run(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        form = DepositForm(data={'amount': slots['amount'], 'mode': slots.get('mode') or '',
                                 'reference': slots.get('reference', ''), 'reason': ''},
                           tenant=ctx.tenant)
        if not form.is_valid():
            raise NotDone([say(_('I could not record that: %(problems)s Type a correction or say '
                                 '"cancel".') % {'problems': form_errors(form)})])
        data = form.cleaned_data
        entry = services.record_deposit(tenancy=stay, user=ctx.user, amount=data['amount'],
                                        reason=data['reason'], mode=data['mode'],
                                        reference=data['reference'])
        text = _('Deposit %(no)s recorded: %(amount)s from %(name)s. Held now: %(held)s.') % {
            'no': entry.receipt_number, 'amount': ctx.money(entry.amount),
            'name': stay.guest.full_name, 'held': ctx.money(stay.deposit_held())}
        if entry.awaits_approval:
            text += ' ' + _('It is waiting for the owner to approve it.')
        return [say(text, buttons=[
            link(_('Deposit receipt'), reverse('guests:deposit_receipt', args=[entry.pk])),
            link(_('Open stay'), reverse('guests:tenancy_detail', args=[stay.pk]))])]
