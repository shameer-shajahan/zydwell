"""A guest leaving: "Ravi left today, refund 500" or "Ravi will leave on 30th"."""
import re
from decimal import Decimal

from django.urls import reverse
from django.utils.translation import gettext as _

from billing import services as billing
from core.constants import MODULE_BILLING, MODULE_GUESTS
from guests import services
from guests.forms import CheckOutForm, NoticeForm

from .. import lookup
from ..brain import parse
from .add_guest import form_errors
from .base import (Finish, NotDone, Skill, as_date, as_decimal, cancel_button, confirm_buttons, day,
                   link, say)
from .common import answer_stay, ask_stay, pick_stay, read, take_stays

TAGS = ('refund', 'deduct')
_REASON = re.compile(
    r'\b(?:deduct|deduction|cut|keep|charge|minus|less)\b[^a-z]*(?:aed|dhs?)?\s*[\d,.]*\s*k?\s*'
    r'(?:for|as|towards|because\s+of|due\s+to)\s+([a-z][a-z /&-]{2,80})', re.I)
_NO_REFUND = re.compile(r'\b(?:no\s+refund|keep\s+(?:the\s+)?(?:whole\s+|full\s+)?deposit|'
                        r'don\'?t\s+refund|not\s+refund)\b', re.I)


def final_rent_choice(tenancy, leaving):
    """What the check-out form would pick for the final period's rent.

    The days stayed, unless that cannot be done here: more than one invoice
    covers the period, or more than those days is already paid.
    """
    final = billing.final_period(tenancy, leaving)
    paid_over = max(billing.final_invoice_floor(final['invoice']) - final['prorata_rent'],
                    Decimal('0'))
    if final['clash'] or paid_over > 0:
        return CheckOutForm.FINAL_KEEP, final
    return CheckOutForm.FINAL_PRORATA, final


class Leaving(Skill):
    """Check-out and notice are one conversation: the date decides which."""

    module = MODULE_GUESTS
    action = 'edit'

    def __init__(self, name):
        self.name = name
        self.example = (_('Ravi left today, refund 500') if name == 'check_out'
                        else _('Ravi will leave on 30th'))

    # ------------------------------------------------------------ reading
    def start(self, ctx, text):
        slots = {'kind': self.name}
        found = read(ctx, text, tags=TAGS, forward=True)
        take_stays(ctx, slots, found)
        self._take(ctx, slots, found)
        return slots

    def preset(self, ctx, values):
        slots = {'kind': self.name}
        if str(values.get('tenancy', '')).isdigit():
            pick_stay(ctx, slots, values['tenancy'])
        return slots

    def _take(self, ctx, slots, found):
        changed = False
        when = found.get('date')
        if when and when <= ctx.today and slots['kind'] == 'give_notice' and re.search(
                r'\bnotice\b', found['text'], re.I) and not slots.get('date'):
            # "gave notice yesterday": the day of the notice, not of leaving.
            slots['notice_on'] = when.isoformat()
            changed = True
        elif when:
            slots['date'] = when.isoformat()
            # A date ahead is notice; today or earlier is the day they left.
            slots['kind'] = 'give_notice' if when > ctx.today else 'check_out'
            changed = True
        tagged = found['tagged']
        if 'deduct' in tagged:
            slots['deduct'] = str(tagged['deduct'])
            match = _REASON.search(found['text'])
            if match:
                slots['reason'] = match.group(1).strip()[:200]
            changed = True
        if 'refund' in tagged:
            slots['refund'] = str(tagged['refund'])
            changed = True
        elif _NO_REFUND.search(found['text']):
            slots['refund'] = '0'
            changed = True
        return changed

    # ------------------------------------------------------------ asking
    def ask(self, ctx, slots):
        question = ask_stay(ctx, slots, _('Who is leaving? Type the guest\'s name.'))
        if question:
            return question
        stay = lookup.stay(ctx, slots['tenancy'])
        if stay.is_team_let:
            raise Finish([say(_('%(name)s is a team let. Change it on the team page.') % {
                'name': stay.guest.full_name},
                buttons=[link(_('Open team'), reverse('teams:team_edit', args=[stay.pk]))])])
        if slots['kind'] == 'give_notice' and not slots.get('date'):
            return 'date', say(_('When is %(name)s leaving? For example "30th" or "15 Dec".') % {
                'name': stay.guest.full_name}, buttons=[cancel_button()])
        if as_decimal(slots.get('deduct')) and not slots.get('reason'):
            return 'reason', say(_('What is the %(amount)s deduction for?') % {
                'amount': ctx.money(slots['deduct'])}, buttons=[cancel_button()])
        return None

    def answer(self, ctx, slots, key, text):
        if key == 'tenancy':
            return answer_stay(ctx, slots, text)
        if key == 'date':
            when, _span = parse.find_date(text, ctx.today, forward=True)
            if not when:
                return say(_('Type the date, like "30th" or "15 Dec".'), buttons=[cancel_button()])
            slots['date'] = when.isoformat()
            slots['kind'] = 'give_notice' if when > ctx.today else 'check_out'
        elif key == 'reason':
            slots['reason'] = text.strip()[:200]
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'tenancy':
            pick_stay(ctx, slots, value)

    def change(self, ctx, slots, text):
        return self._take(ctx, slots, read(ctx, text, tags=TAGS, forward=True))

    # ------------------------------------------------------------ saving
    def _refund(self, stay, slots):
        held = stay.deposit_held()
        if slots.get('refund') not in (None, ''):
            return as_decimal(slots['refund'])
        # The check-out form offers the whole deposit back unless told otherwise.
        return max(held - (as_decimal(slots.get('deduct')) or Decimal('0')), Decimal('0'))

    def summary(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        when = as_date(slots.get('date')) or ctx.today
        rows = [(_('Guest'), stay.guest.full_name),
                (_('Bed'), stay.unit_label() or stay.location_label())]
        if slots['kind'] == 'give_notice':
            rows += [(_('Notice given on'), day(as_date(slots.get('notice_on')) or ctx.today)),
                     (_('Leaving on'), day(when))]
            return say(_('Record this notice? The bed stays occupied until the check-out.'),
                       rows=rows, buttons=confirm_buttons(_('Yes, record notice')))

        rows.append((_('Check-out date'), day(when)))
        owed = stay.outstanding()
        if owed > 0:
            rows.append((_('Rent owed now'), ctx.money(owed)))
        if ctx.allowed(MODULE_BILLING, 'create') and when >= stay.check_in_date:
            choice, final = final_rent_choice(stay, when)
            rows.append((_('Final rent'), _('Only the %(days)s days stayed: %(amount)s') % {
                'days': final['days'], 'amount': ctx.money(final['prorata_rent'])}
                if choice == CheckOutForm.FINAL_PRORATA else _('No change, as already billed')))
        rows.append((_('Deposit held'), ctx.money(stay.deposit_held())))
        if as_decimal(slots.get('deduct')):
            rows.append((_('Deduct'), f"{ctx.money(slots['deduct'])} · {slots.get('reason', '')}"))
        refund = self._refund(stay, slots)
        rows.append((_('Refund to guest'), ctx.money(refund)))
        text = _('Please check the check-out. To change something, type it, for example '
                 '"refund 300" or "deduct 200 for cleaning". For other final-rent choices use the '
                 'full check-out form.')
        out = refund + (as_decimal(slots.get('deduct')) or Decimal('0'))
        if out > stay.deposit_held():
            text = _('The refund and deduction (%(out)s) are more than the deposit held '
                     '(%(held)s). Type a smaller refund, for example "refund %(held_plain)s".') % {
                'out': ctx.money(out), 'held': ctx.money(stay.deposit_held()),
                'held_plain': f'{stay.deposit_held():.0f}'}
        return say(text, rows=rows,
            buttons=confirm_buttons(_('Yes, check out')) + [
                link(_('Full check-out form'), reverse('guests:check_out', args=[stay.pk])
                     + f'?date={when.isoformat()}')])

    def run(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        when = as_date(slots.get('date')) or ctx.today
        open_stay = [link(_('Open stay'), reverse('guests:tenancy_detail', args=[stay.pk]))]
        if slots['kind'] == 'give_notice':
            notice_on = as_date(slots.get('notice_on')) or ctx.today
            form = NoticeForm(data={'notice_given_on': notice_on.isoformat(),
                                    'expected_check_out': when.isoformat()})
            if not form.is_valid():
                raise NotDone([say(_('I could not record that: %(problems)s') % {
                    'problems': form_errors(form)})])
            services.give_notice(tenancy=stay, user=ctx.user,
                                 notice_date=form.cleaned_data['notice_given_on'],
                                 expected_check_out=form.cleaned_data['expected_check_out'])
            return [say(_('Notice recorded: %(name)s leaves on %(date)s.') % {
                'name': stay.guest.full_name, 'date': day(when)}, buttons=open_stay)]

        can_bill = ctx.allowed(MODULE_BILLING, 'create')
        choice = CheckOutForm.FINAL_KEEP
        if can_bill and when >= stay.check_in_date:
            choice, _final = final_rent_choice(stay, when)
        form = CheckOutForm(data={
            'check_out_date': when.isoformat(),
            'deduction_amount': slots.get('deduct') or '0',
            'deduction_reason': slots.get('reason', ''),
            'refund_amount': str(self._refund(stay, slots)), 'note': '',
            'final_charge': choice, 'final_amount': '', 'collect_amount': '',
            'collect_mode': '', 'collect_reference': '',
        }, tenancy=stay, can_bill=can_bill)
        if not form.is_valid():
            raise NotDone([say(_('I could not check out: %(problems)s Type a correction or say '
                                 '"cancel".') % {'problems': form_errors(form)})])
        services.check_out_and_settle(tenant=ctx.tenant, user=ctx.user, tenancy=stay,
                                      data=form.cleaned_data, can_bill=can_bill)
        stay.refresh_from_db()
        text = _('Done! %(name)s is checked out and the bed is free.') % {
            'name': stay.guest.full_name}
        owed = stay.outstanding()
        if owed > 0:
            text += ' ' + _('%(amount)s is still owed.') % {'amount': ctx.money(owed)}
        return [say(text, buttons=open_stay)]
