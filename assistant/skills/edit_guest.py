"""Change a guest's details: "change Ravi mobile to 0509876543", "Akash nationality Pakistani"."""
from django.forms.models import model_to_dict
from django.urls import reverse
from django.utils.translation import gettext as _

from core import audit
from core.constants import MODULE_GUESTS
from core.models import AuditLog
from guests.forms import GuestForm
from guests.models import Guest

from .. import lookup
from ..brain import entities
from .add_guest import FIELDS, _clean, _labels, form_errors
from .base import NotDone, Skill, cancel_button, confirm_buttons, link, say
from .common import answer_guest, ask_guest, pick_guest, read, take_guests


def _changes(text):
    """The new details in a message; a new name only after "name is/to"."""
    return _clean(entities.extract(text, name='marker'))


def _target_text(text):
    """The part of a message that names whose details change: before the new name."""
    marker = entities.NAME_MARKER.search(text or '')
    return text[:marker.start()] if marker else text


class EditGuest(Skill):
    name = 'edit_guest'
    module = MODULE_GUESTS
    action = 'create'          # the same right as the guest's edit page
    title = _('Change guest details')
    example = _('change Ravi mobile to 0509876543')

    def start(self, ctx, text):
        slots = {'changes': _changes(text)}
        target = _target_text(text)
        found = read(ctx, target)
        take_guests(ctx, slots, found)
        return slots

    def preset(self, ctx, values):
        slots = {'changes': {}}
        if str(values.get('guest', '')).isdigit():
            pick_guest(ctx, slots, values['guest'])
        return slots

    def ask(self, ctx, slots):
        question = ask_guest(ctx, slots, _('Whose details? Type the guest\'s name.'))
        if question:
            return question
        if not slots['changes']:
            guest = lookup.guest(ctx, slots['guest'])
            return 'changes', say(
                _('What should I change for %(name)s? For example "mobile 0509876543", '
                  '"nationality Indian", "works at Emaar" or "name is Ravi Kumar".') % {
                    'name': guest.full_name}, buttons=[cancel_button()])
        return None

    def answer(self, ctx, slots, key, text):
        if key == 'guest':
            return answer_guest(ctx, slots, text)
        changes = _changes(text)
        if not changes:
            return say(_('I could not find a detail in that. Try "mobile 0509876543" or '
                         '"name is Ravi Kumar".'), buttons=[cancel_button()])
        slots['changes'].update(changes)
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'guest':
            pick_guest(ctx, slots, value)

    def change(self, ctx, slots, text):
        changes = _changes(text)
        slots['changes'].update(changes)
        return bool(changes)

    def summary(self, ctx, slots):
        guest = lookup.guest(ctx, slots['guest'])
        labels = _labels()
        gender = dict(Guest._meta.get_field('gender').choices)
        rows = []
        for field in FIELDS:
            if field in slots['changes']:
                old = getattr(guest, field) or '—'
                new = slots['changes'][field]
                if field == 'gender':
                    old, new = gender.get(old, old), gender.get(new, new)
                rows.append((labels[field], f'{old} → {new}'))
        return say(_('Change %(name)s\'s details?') % {'name': guest.full_name}, rows=rows,
                   buttons=confirm_buttons(_('Yes, save')))

    def run(self, ctx, slots):
        guest = lookup.guest(ctx, slots['guest'])
        before = audit.snapshot(guest)
        form = GuestForm(instance=guest)
        data = model_to_dict(guest, fields=[f for f in form.fields if f != 'photo'])
        data = {k: ('' if v is None else v) for k, v in data.items()}
        if data.get('date_of_birth'):
            data['date_of_birth'] = data['date_of_birth'].isoformat()
        data.update(slots['changes'])
        form = GuestForm(data=data, instance=guest)
        if not form.is_valid():
            raise NotDone([say(_('I could not save that: %(problems)s Type a correction or say '
                                 '"cancel".') % {'problems': form_errors(form)})])
        guest = form.save()
        audit.log(AuditLog.ACTION_UPDATE, guest, actor=ctx.user, request=ctx.request,
                  summary=_('Guest %(name)s saved') % {'name': guest.full_name},
                  changes=audit.diff(before, audit.snapshot(guest)))
        return [say(_('Saved. %(name)s\'s details are up to date.') % {'name': guest.full_name},
                    buttons=[link(_('Open guest'), reverse('guests:guest_detail',
                                                           args=[guest.pk]))])]
