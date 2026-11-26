"""Record rent: "Ravi paid 800 cash", "received 1500 from Ahmed by card ref 4471"."""
import re
from decimal import Decimal

from django.urls import reverse
from django.utils.translation import gettext as _

from accounting.models import KIND_INCOME
from billing import services as billing
from billing.forms import PaymentForm
from billing.models import Invoice, PaymentMode
from core.constants import MODULE_BILLING

from .. import lookup
from ..brain import parse
from .add_guest import form_errors
from .base import (Handoff, NotDone, Skill, as_date, as_decimal, cancel_button, choose,
                   confirm_buttons, day, link, say)
from .common import answer_stay, ask_stay, pick_stay, read, stay_place, take_stays


def open_invoices(tenancy):
    """Unpaid invoices of a stay, oldest first — the order "collect all" uses."""
    return list(billing.owing(Invoice.objects.filter(tenancy=tenancy)).order_by(
        'due_date', 'period_start', 'pk'))


def _covering(invoices, amount):
    """The oldest invoices that together take ``amount``."""
    chosen, total = [], Decimal('0')
    for invoice in invoices:
        if total >= amount:
            break
        chosen.append(invoice)
        total += invoice.balance
    return chosen


class Payment(Skill):
    name = 'payment'
    module = MODULE_BILLING
    action = 'create'
    title = _('Record a payment')
    example = _('Ravi paid 800 cash')

    def start(self, ctx, text):
        slots = {}
        found = read(ctx, text, forward=False)
        if re.search(r'\b(?:deposit|security)\b', found['text'], re.I):
            raise Handoff('deposit')              # "akash paid deposit 500"
        take_stays(ctx, slots, found)
        if not (slots.get('tenancy') or slots.get('tenancy_options') or slots.get('who')) and \
                lookup.find_category(ctx, found['text'], kind=KIND_INCOME):
            raise Handoff('income')               # "received 100 for laundry"
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
        if found['amounts']:
            slots['amount'] = str(found['amounts'][0])
            slots.pop('settled', None)
            changed = True
        mode = lookup.find_mode(ctx, found['text'])
        if mode is not None:
            slots['mode'] = mode.pk
            changed = True
        if found.get('reference'):
            slots['reference'] = found['reference']
            changed = True
        if found.get('date'):
            mode_obj = lookup.mode(ctx, slots.get('mode')) if slots.get('mode') else None
            if mode_obj is not None and mode_obj.needs_due_date and not slots.get('cheque_due'):
                slots['cheque_due'] = found['date'].isoformat()
            else:
                slots['paid_on'] = found['date'].isoformat()
            changed = True
        return changed

    def _stay(self, ctx, slots):
        return lookup.stay(ctx, slots['tenancy'])

    # ------------------------------------------------------------ asking
    def ask(self, ctx, slots):
        question = ask_stay(ctx, slots, _('Who paid? Type the guest\'s name.'))
        if question:
            return question
        stay = self._stay(ctx, slots)
        owed = stay.outstanding()
        amount = as_decimal(slots.get('amount'))
        if not amount or amount <= 0:
            buttons = []
            if owed > 0:
                buttons.append(choose(_('%(amount)s (all owed)') % {'amount': ctx.money(owed)},
                                      'amount', owed))
            text = _('How much did %(name)s pay?') % {'name': stay.guest.full_name}
            if owed > 0:
                text += ' ' + _('They owe %(amount)s.') % {'amount': ctx.money(owed)}
            return 'amount', say(text, buttons=buttons + [cancel_button()])

        mode = lookup.mode(ctx, slots['mode']) if slots.get('mode') else None
        if mode is None:
            mode = lookup.default_mode(ctx)
            slots['mode'] = mode.pk if mode else None
        if mode is not None and mode.settles_from_credit:
            slots.pop('mode')
            modes = PaymentMode.objects.filter(tenant=ctx.tenant, is_active=True,
                                               settles_from_credit=False)
            return 'mode', say(_('Advance credit is spent from the invoice page. How was this '
                                 'money paid?'),
                               buttons=[choose(m.name, 'mode', m.pk) for m in modes]
                               + [cancel_button()])
        if mode is not None and mode.needs_reference and not slots.get('reference'):
            return 'reference', say(_('What is the %(mode)s reference number?') % {
                'mode': mode.name.lower()}, buttons=[cancel_button()])
        if mode is not None and mode.needs_due_date and not slots.get('cheque_due'):
            return 'cheque_due', say(_('When is the cheque due? For example "30 Dec".'),
                                     buttons=[cancel_button()])
        if owed > 0 and amount > owed and not slots.get('settled'):
            return 'excess', say(
                _('%(name)s owes only %(owed)s. What should I do with %(amount)s?') % {
                    'name': stay.guest.full_name, 'owed': ctx.money(owed),
                    'amount': ctx.money(amount)},
                buttons=[choose(_('Record %(owed)s') % {'owed': ctx.money(owed)}, 'excess', 'owed'),
                         choose(_('Keep all %(amount)s as advance') % {'amount': ctx.money(amount)},
                                'excess', 'advance'),
                         cancel_button()])
        return None

    # ------------------------------------------------------------ replies
    def answer(self, ctx, slots, key, text):
        if key == 'tenancy':
            return answer_stay(ctx, slots, text)
        found = read(ctx, text, forward=key != 'cheque_due')
        if key == 'amount':
            if not found['amounts']:
                return say(_('Type the amount as a number, like 800.'), buttons=[cancel_button()])
            self._take(ctx, slots, found)
        elif key == 'mode':
            mode = lookup.find_mode(ctx, text)
            if mode is None:
                return say(_('Pick how it was paid.'))
            slots['mode'] = mode.pk
        elif key == 'reference':
            reference = found.get('reference') or text.strip().split()[-1] if text.strip() else ''
            slots['reference'] = reference.upper()[:80]
        elif key == 'cheque_due':
            when, _span = parse.find_date(text, ctx.today, forward=True)
            if not when:
                return say(_('Type the date, like "30 Dec" or "30/12".'), buttons=[cancel_button()])
            slots['cheque_due'] = when.isoformat()
        elif key == 'excess':
            lowered = text.lower()
            if 'advance' in lowered or 'all' in lowered:
                self.pick(ctx, slots, 'excess', 'advance')
            elif found['amounts']:
                self._take(ctx, slots, found)
            else:
                return say(_('Pick one of the choices, or type a new amount.'))
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'tenancy':
            pick_stay(ctx, slots, value)
        elif key == 'amount':
            amount = as_decimal(value)
            if amount and amount > 0:
                slots['amount'] = str(amount)
        elif key == 'mode' and str(value).isdigit():
            mode = lookup.mode(ctx, int(value))
            if mode is not None and not mode.settles_from_credit:
                slots['mode'] = mode.pk
        elif key == 'excess':
            stay = self._stay(ctx, slots)
            if value == 'owed' and stay is not None:
                slots['amount'] = str(stay.outstanding())
            slots['settled'] = value

    def change(self, ctx, slots, text):
        return self._take(ctx, slots, read(ctx, text, forward=False))

    # ------------------------------------------------------------ saving
    def _plan(self, ctx, slots):
        stay = self._stay(ctx, slots)
        amount = as_decimal(slots['amount'])
        invoices = [] if slots.get('settled') == 'advance' else open_invoices(stay)
        return stay, amount, _covering(invoices, amount)

    def summary(self, ctx, slots):
        stay, amount, invoices = self._plan(ctx, slots)
        mode = lookup.mode(ctx, slots['mode'])
        rows = [
            (_('Guest'), stay.guest.full_name),
            (_('Bed'), stay_place(stay)),
            (_('Amount'), ctx.money(amount)),
            (_('Paid by'), mode.name if mode else '—'),
        ]
        if slots.get('reference'):
            rows.append((_('Reference'), slots['reference']))
        if slots.get('cheque_due'):
            rows.append((_('Cheque due'), day(slots['cheque_due'])))
        rows.append((_('Paid on'), day(as_date(slots.get('paid_on')) or ctx.today)))
        if invoices:
            rows.append((_('For'), ', '.join(
                f'{inv.number} ({inv.period_label()})' for inv in invoices)))
        else:
            rows.append((_('For'), _('Advance credit (nothing is owed now)')))
        text = _('Please check the payment. To change something, type it, for example '
                 '"900 by card".')
        if billing.needs_approval(ctx.user):
            text += ' ' + _('It will wait for the owner to approve it.')
        return say(text, rows=rows, buttons=confirm_buttons(_('Yes, record payment')))

    def run(self, ctx, slots):
        stay, amount, invoices = self._plan(ctx, slots)
        paid_on = as_date(slots.get('paid_on')) or ctx.today
        form = PaymentForm(data={
            'tenancy': stay.pk, 'invoices': [inv.pk for inv in invoices], 'invoice': '',
            'amount': str(amount), 'mode': slots['mode'], 'reference': slots.get('reference', ''),
            'cheque_due_date': slots.get('cheque_due') or '', 'paid_on': paid_on.isoformat(),
            'note': '',
        }, tenant=ctx.tenant, user=ctx.user)
        if not form.is_valid():
            raise NotDone([say(_('I could not record that: %(problems)s Type a correction or say '
                                 '"cancel".') % {'problems': form_errors(form)})])
        payment, applied = billing.save_payment(tenant=ctx.tenant, user=ctx.user,
                                                data=form.cleaned_data)
        text = _('Receipt %(no)s recorded: %(amount)s from %(name)s.') % {
            'no': payment.receipt_number, 'amount': ctx.money(payment.amount),
            'name': stay.guest.full_name}
        if payment.awaits_approval:
            text += ' ' + _('It is waiting for the owner to approve it.')
        if applied:
            text += ' ' + _('Advance credit was applied to the oldest unpaid invoices.')
        left = stay.outstanding()
        if left > 0:
            text += ' ' + _('%(name)s still owes %(amount)s.') % {
                'name': stay.guest.full_name, 'amount': ctx.money(left)}
        return [say(text, buttons=[
            link(_('View receipt'), reverse('billing:receipt', args=[payment.pk])),
            link(_('Payment details'), reverse('billing:payment_detail', args=[payment.pk])),
        ])]
