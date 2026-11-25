"""Correct a stay's check-in date: "Akash actually came on 20th", "change his check in to 1 Nov"."""
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.translation import gettext as _

from core.constants import MODULE_BILLING, MODULE_GUESTS
from guests import services

from .. import lookup
from ..brain import parse
from .base import (Finish, Handoff, NotDone, Skill, as_date, cancel_button, confirm_buttons,
                   day, link, say)
from .common import answer_stay, ask_stay, pick_stay, read, stay_place, take_stays


def _read_date(ctx, found, text):
    """A corrected check-in is nearly always in the past: "20th" is the last one."""
    if found.get('date'):
        return found['date']
    numbers = [n for n in found['amounts'] if n == int(n) and 1 <= n <= 31]
    if numbers:
        return parse.day_of_month(int(numbers[0]), ctx.today)
    return None


class EditCheckIn(Skill):
    name = 'edit_check_in'
    module = MODULE_GUESTS
    action = 'edit'
    title = _('Change check-in date')
    example = _('change Ravi check-in date to 20 Nov')

    def start(self, ctx, text):
        slots = {}
        found = read(ctx, text, forward=False)
        take_stays(ctx, slots, found)
        if not (slots.get('tenancy') or slots.get('tenancy_options')) and lookup.find_guests(
                ctx, found['rest'], found.get('mobile', '')):
            # A guest who is not staying anywhere has no check-in to change:
            # "check in akash date 1" is a new check-in.
            raise Handoff('check_in')
        when = _read_date(ctx, found, text)
        if when:
            slots['date'] = when.isoformat()
        return slots

    def preset(self, ctx, values):
        slots = {}
        if str(values.get('tenancy', '')).isdigit():
            pick_stay(ctx, slots, values['tenancy'])
        return slots

    def ask(self, ctx, slots):
        question = ask_stay(ctx, slots, _('Whose check-in date? Type the guest\'s name.'))
        if question:
            return question
        stay = lookup.stay(ctx, slots['tenancy'])
        if stay.is_team_let:
            raise Finish([say(_('%(name)s is a team let. Change it on the team page.') % {
                'name': stay.guest.full_name},
                buttons=[link(_('Open team'), reverse('teams:team_edit', args=[stay.pk]))])])
        if not slots.get('date'):
            return 'date', say(_('What is the right check-in date for %(name)s? It is %(date)s '
                                 'now.') % {'name': stay.guest.full_name,
                                            'date': day(stay.check_in_date)},
                               buttons=[cancel_button()])
        when = as_date(slots['date'])
        if when == stay.check_in_date:
            slots.pop('date')
            return 'date', say(_('%(date)s is already the check-in date. Type the right one.') % {
                'date': day(when)}, buttons=[cancel_button()])
        problem = services.check_in_date_problem(stay, when)
        if problem:
            slots.pop('date')
            return 'date', say(problem + ' ' + _('Type another date.'), buttons=[cancel_button()])
        return None

    def answer(self, ctx, slots, key, text):
        if key == 'tenancy':
            return answer_stay(ctx, slots, text)
        when = _read_date(ctx, read(ctx, text, forward=False), text)
        if not when:
            return say(_('Type the date, like "20 Nov" or "20/11".'), buttons=[cancel_button()])
        slots['date'] = when.isoformat()
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'tenancy':
            pick_stay(ctx, slots, value)

    def change(self, ctx, slots, text):
        when = _read_date(ctx, read(ctx, text, forward=False), text)
        if when:
            slots['date'] = when.isoformat()
            return True
        return False

    def summary(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        when = as_date(slots['date'])
        rows = [(_('Guest'), stay.guest.full_name),
                (_('Bed'), stay_place(stay)),
                (_('Check-in date'), f'{day(stay.check_in_date)} → {day(when)}')]
        invoice, start, rent, reason = services.plan_first_invoice(stay, stay.check_in_date, when)
        if invoice is not None:
            if rent is None:
                rows.append((_('First invoice'), reason))
            elif not ctx.allowed(MODULE_BILLING, 'edit'):
                rows.append((_('First invoice'), _('%(no)s stays as it is (changing invoices '
                                                   'needs billing rights)') % {
                    'no': invoice.number}))
            else:
                rows.append((_('First invoice'), _('%(no)s will run from %(date)s: rent %(amount)s')
                             % {'no': invoice.number, 'date': day(start),
                                'amount': ctx.money(rent)}))
        return say(_('Change the check-in date?'), rows=rows,
                   buttons=confirm_buttons(_('Yes, change it')))

    def run(self, ctx, slots):
        stay = lookup.stay(ctx, slots['tenancy'])
        when = as_date(slots['date'])
        try:
            note = services.change_check_in_date(
                tenancy=stay, user=ctx.user, new_date=when,
                rebill=ctx.allowed(MODULE_BILLING, 'edit'))
        except ValidationError as error:
            raise NotDone([say(' '.join(error.messages) + ' ' + _('Type another date or say '
                                                                  '"cancel".'))])
        text = _('Done! %(name)s\'s check-in date is now %(date)s.') % {
            'name': stay.guest.full_name, 'date': day(when)}
        if note:
            text += ' ' + note
        return [say(text, buttons=[
            link(_('Open stay'), reverse('guests:tenancy_detail', args=[stay.pk]))])]
