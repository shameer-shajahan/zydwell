"""Change what was agreed for a stay: "Shameer's deposit is 200", "rent 700", "bill day 5"."""
import re
from decimal import Decimal, InvalidOperation

from django.forms.models import model_to_dict
from django.urls import reverse
from django.utils.translation import gettext as _

from core import audit
from core.constants import MODULE_GUESTS
from core.models import AuditLog
from guests.forms import TenancyEditForm

from .. import lookup
from ..brain import parse
from .add_guest import form_errors
from .base import (Finish, NotDone, Skill, cancel_button, confirm_buttons, link, say, start)
from .common import answer_stay, ask_stay, pick_stay, read, stay_place, take_stays

_SAY = r'(?:\s+(?:amount|agreed))?\s*(?:is|=|:|to|as|should\s+be|will\s+be|of|now)?\s*(?:aed|dhs?)?\s*'
_NUMBER = r'(\d[\d,]*(?:\.\d{1,2})?)'
DEPOSIT = re.compile(r'\b(?:deposit|security)' + _SAY + _NUMBER, re.I)
RENT = re.compile(r'\b(?:monthly\s+)?rent' + _SAY + _NUMBER, re.I)
BILL_DAY = re.compile(
    r'\b(?:bill(?:ing)?|rent|invoice|due|pay(?:ment)?)\s+(?:day|date)' + _SAY.replace('(?:aed|dhs?)?', '')
    + r'(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b', re.I)
LABELS = {'deposit_amount': _('Deposit agreed'), 'monthly_rent': _('Monthly rent'),
          'billing_day': _('Bill day')}
FIELDS = ['check_in_date', 'monthly_rent', 'rent_mode', 'prorate_first_period', 'billing_cycle',
          'billing_day', 'expected_check_out', 'deposit_amount', 'first_invoice_amount',
          'agreement_note']


def _amount(match):
    try:
        return str(Decimal(match.group(1).replace(',', '')))
    except InvalidOperation:
        return None


def read_terms(text):
    """The agreed terms a message changes: ``{'deposit_amount', 'monthly_rent', 'billing_day'}``."""
    text = parse.normalise(text or '')
    changes = {}
    deposit = DEPOSIT.search(text)
    if deposit and _amount(deposit) is not None:
        changes['deposit_amount'] = _amount(deposit)
    day = BILL_DAY.search(text)
    if day and 1 <= int(day.group(1)) <= 31:
        changes['billing_day'] = min(int(day.group(1)), 28)
        text = text[:day.start()] + ' ' + text[day.end():]   # "rent day 5" is not rent 5
    rent = RENT.search(text)
    if rent and _amount(rent) is not None:
        changes['monthly_rent'] = _amount(rent)
    return changes


class EditTerms(Skill):
    name = 'edit_terms'
    module = MODULE_GUESTS
    action = 'edit'
    title = _('Change rent, deposit or bill day')
    example = _("Shameer's deposit is 200 / rent 700 / bill day 5")

    def start(self, ctx, text):
        slots = {'changes': read_terms(text)}
        take_stays(ctx, slots, read(ctx, text))
        return slots

    def preset(self, ctx, values):
        slots = {'changes': {}}
        if str(values.get('tenancy', '')).isdigit():
            pick_stay(ctx, slots, values['tenancy'])
        return slots

    def _owner(self, ctx):
        return ctx.user.is_admin or ctx.user.is_super_admin

    def ask(self, ctx, slots):
        question = ask_stay(ctx, slots, _('Whose stay? Type the guest\'s name.'))
        if question:
            return question
        stay = lookup.stay(ctx, slots['tenancy'])
        if stay.is_team_let:
            raise Finish([say(_('%(name)s is a team let. Change it on the team page.') % {
                'name': stay.guest.full_name},
                buttons=[link(_('Open team'), reverse('teams:team_edit', args=[stay.pk]))])])
        changes = slots['changes']
        if 'monthly_rent' in changes and not self._owner(ctx):
            changes.pop('monthly_rent')
            slots['note'] = _('Only the owner can change the rent.')
        if not changes:
            return 'changes', say(' '.join(filter(None, [slots.pop('note', ''), _(
                'What should change for %(name)s? For example "deposit 500", "rent 700" or '
                '"bill day 5".') % {'name': stay.guest.full_name}])), buttons=[cancel_button()])
        return None

    def answer(self, ctx, slots, key, text):
        if key == 'tenancy':
            return answer_stay(ctx, slots, text)
        changes = read_terms(text)
        if not changes:
            return say(_('Type the change, like "deposit 500", "rent 700" or "bill day 5".'),
                       buttons=[cancel_button()])
        slots['changes'].update(changes)
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'tenancy':
            pick_stay(ctx, slots, value)

    def change(self, ctx, slots, text):
        changes = read_terms(text)
        slots['changes'].update(changes)
        return bool(changes)

    def summary(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        rows = [(_('Guest'), stay.guest.full_name), (_('Bed'), stay_place(stay))]
        for field, new in slots['changes'].items():
            old = getattr(stay, field)
            if field == 'billing_day':
                rows.append((LABELS[field], _('day %(old)s → day %(new)s') % {'old': old, 'new': new}))
            else:
                rows.append((LABELS[field], f'{ctx.money(old)} → {ctx.money(new)}'))
        text = _('Change what was agreed for %(name)s?') % {'name': stay.guest.full_name}
        if slots.get('note'):
            text = slots['note'] + ' ' + text
        buttons = confirm_buttons(_('Yes, change it'))
        if 'deposit_amount' in slots['changes']:
            text += ' ' + _('This is the deposit the guest should pay, not money received.')
            buttons.append(start(_('Record %(amount)s received instead') % {
                'amount': ctx.money(slots['changes']['deposit_amount'])}, 'deposit',
                tenancy=stay.pk, amount=slots['changes']['deposit_amount']))
        if 'billing_day' in slots['changes']:
            text += ' ' + _('Invoices already raised stay as they are.')
        return say(text, rows=rows, buttons=buttons)

    def run(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        audited = ['monthly_rent', 'billing_day', 'deposit_amount']
        before = audit.snapshot(stay, audited)
        data = model_to_dict(stay, fields=FIELDS)
        data = {k: ('' if v is None else v) for k, v in data.items()}
        for field in ('check_in_date', 'expected_check_out'):
            if data.get(field):
                data[field] = data[field].isoformat()
        if data.get('prorate_first_period'):
            data['prorate_first_period'] = 'on'
        else:
            data.pop('prorate_first_period', None)
        data.update(slots['changes'])
        form = TenancyEditForm(data=data, instance=stay)
        if not self._owner(ctx):
            form.fields['monthly_rent'].disabled = True     # as on the edit page
        if not form.is_valid():
            raise NotDone([say(_('I could not save that: %(problems)s Type a correction or say '
                                 '"cancel".') % {'problems': form_errors(form)})])
        stay = form.save()
        audit.log(AuditLog.ACTION_UPDATE, stay, actor=ctx.user, request=ctx.request,
                  summary=_('Tenancy updated for %(name)s') % {'name': stay.guest.full_name},
                  changes=audit.diff(before, audit.snapshot(stay, audited)))
        return [say(_('Saved. %(name)s\'s stay is updated.') % {'name': stay.guest.full_name},
                    buttons=[link(_('Open stay'), reverse('guests:tenancy_detail',
                                                          args=[stay.pk]))])]
