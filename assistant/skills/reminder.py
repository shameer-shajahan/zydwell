"""Rent reminders on WhatsApp: "send reminder to Ravi", "remind everyone who has not paid".

The assistant never sends a message by itself. It hands over a button that
opens WhatsApp with the reminder written, through the same quick-send link
the overdue page uses, so a person always presses Send.
"""
from urllib.parse import urlencode

from django.urls import reverse
from django.utils.translation import gettext as _

from billing import services as billing
from billing.models import Invoice
from core.constants import MODULE_MESSAGING

from .. import lookup
from .base import Skill, link, say
from .common import answer_stay, ask_stay, pick_stay, read, take_stays

ALL = ('all', 'everyone', 'everybody', 'every one', 'all guests', 'all tenants', 'overdue',
       'defaulters', 'who has not paid', 'pending')
LIMIT = 8


def _reminder_link(ctx, stay, invoice):
    code = 'rent_overdue' if invoice.due_date < ctx.today else 'rent_reminder'
    query = urlencode({'guest': stay.guest_id, 'invoice': invoice.pk, 'template': code})
    return f"{reverse('messaging:quick_send')}?{query}"


def _oldest_unpaid(stay):
    return billing.owing(Invoice.objects.filter(tenancy=stay)).order_by('due_date', 'pk').first()


class Reminder(Skill):
    name = 'reminder'
    module = MODULE_MESSAGING
    action = 'create'
    confirm = False
    title = _('Send a rent reminder')
    example = _('send reminder to Ravi / remind everyone who has not paid')

    def start(self, ctx, text):
        found = read(ctx, text)
        slots = {}
        take_stays(ctx, slots, found)
        low = found['text'].lower()
        if not (slots.get('tenancy') or slots.get('tenancy_options') or slots.get('who')) and \
                any(word in low for word in ALL):
            slots['everyone'] = True
        return slots

    def preset(self, ctx, values):
        slots = {}
        if str(values.get('tenancy', '')).isdigit():
            pick_stay(ctx, slots, values['tenancy'])
        return slots

    def ask(self, ctx, slots):
        if slots.get('everyone'):
            return None
        return ask_stay(ctx, slots, _('Who should I remind? Type the guest\'s name, or '
                                      '"everyone".'))

    def answer(self, ctx, slots, key, text):
        if any(word in text.lower() for word in ALL):
            slots['everyone'] = True
            return None
        return answer_stay(ctx, slots, text)

    def pick(self, ctx, slots, key, value):
        pick_stay(ctx, slots, value)

    def run(self, ctx, slots):
        if slots.get('everyone'):
            return self._everyone(ctx)
        stay = lookup.stay(ctx, slots['tenancy'])
        invoice = _oldest_unpaid(stay)
        if invoice is None:
            return [say(_('%(name)s owes nothing, so there is nothing to remind.') % {
                'name': stay.guest.full_name})]
        owed = stay.outstanding()
        late = (ctx.today - invoice.due_date).days
        text = (_('%(name)s owes %(amount)s, %(days)s days late.') if late > 0 else
                _('%(name)s owes %(amount)s, due %(date)s.')) % {
            'name': stay.guest.full_name, 'amount': ctx.money(owed), 'days': late,
            'date': invoice.due_date.strftime('%d %b')}
        text += ' ' + _('Tap below to open WhatsApp with the reminder written, then press Send.')
        return [say(text, buttons=[link(_('Remind %(name)s on WhatsApp') % {
            'name': stay.guest.full_name}, _reminder_link(ctx, stay, invoice), new_tab=True)])]

    def _everyone(self, ctx):
        late = []
        for stay in lookup.live_stays(ctx):
            invoice = _oldest_unpaid(stay)
            if invoice is not None and invoice.due_date < ctx.today:
                late.append((invoice.due_date, stay, invoice))
        if not late:
            return [say(_('Nobody is late with rent, so there is no one to remind.'))]
        late.sort(key=lambda row: row[0])
        text = _('%(count)s guests are late. Tap each one to open WhatsApp with the reminder '
                 'written, then press Send.') % {'count': len(late)}
        if len(late) > LIMIT:
            text += ' ' + _('Here are the %(n)s most overdue.') % {'n': LIMIT}
        buttons = [link(_('Remind %(name)s (%(amount)s)') % {
            'name': stay.guest.full_name, 'amount': ctx.money(stay.outstanding())},
            _reminder_link(ctx, stay, invoice), new_tab=True) for _due, stay, invoice in late[:LIMIT]]
        return [say(text, buttons=buttons)]
