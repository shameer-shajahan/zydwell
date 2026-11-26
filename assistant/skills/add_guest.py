"""Add a guest: "add guest Ravi Kumar 0501234567 Indian"."""
import re

from django.urls import reverse
from django.utils.translation import gettext as _

from core import audit
from core.constants import MODULE_GUESTS
from core.models import AuditLog
from guests.forms import GuestForm
from guests.models import Guest

from ..brain import classifier, entities
from .base import Finish, NotDone, Skill, cancel_button, confirm_buttons, link, say, send, start

REQUIRED = ['full_name', 'mobile']
FIELDS = ['full_name', 'mobile', 'nationality', 'gender', 'email', 'employer']
LIMITS = {'full_name': 150, 'mobile': 32, 'nationality': 80, 'email': 254, 'employer': 150}


def _clean(found):
    return {f: str(v).strip()[:LIMITS.get(f, 150)] for f, v in found.items()
            if f in FIELDS and str(v).strip()}


def _labels():
    return {'full_name': _('Name'), 'mobile': _('Mobile'), 'nationality': _('Nationality'),
            'gender': _('Gender'), 'email': _('Email'), 'employer': _('Employer')}


def _duplicate(ctx, mobile):
    tail = re.sub(r'\D', '', mobile)[-4:]
    if not tail:
        return None
    rows = Guest.objects.filter(tenant=ctx.tenant, is_company=False, mobile__contains=tail)
    return next((g for g in rows if entities.same_mobile(g.mobile, mobile)), None)


class AddGuest(Skill):
    name = 'add_guest'
    module = MODULE_GUESTS
    action = 'create'
    title = _('Add a guest')
    example = _('add guest Ravi Kumar 0501234567 Indian')

    def start(self, ctx, text):
        return {'guest': _clean(entities.extract(text, name='guess'))}

    def preset(self, ctx, values):
        return {'guest': _clean(values)}

    def ask(self, ctx, slots):
        details = slots['guest']
        if not details.get('full_name'):
            return 'full_name', say(_("What is the guest's full name?"), buttons=[cancel_button()])
        if not details.get('mobile'):
            return 'mobile', say(_("What is %(name)s's mobile number?") % {
                'name': details['full_name']}, buttons=[cancel_button()])
        if not slots.get('allow_duplicate'):
            existing = _duplicate(ctx, details['mobile'])
            if existing:
                return 'duplicate', say(
                    _('%(name)s already has the mobile %(mobile)s. Add a new guest anyway?') % {
                        'name': existing.full_name, 'mobile': existing.mobile},
                    buttons=[send(_('Yes, add new guest'), 'yes'),
                             link(_('Open %(name)s') % {'name': existing.full_name},
                                  reverse('guests:guest_detail', args=[existing.pk])),
                             cancel_button()])
        return None

    def answer(self, ctx, slots, key, text):
        details = slots['guest']
        if key == 'duplicate':
            found = _clean(entities.extract(text, name='marker'))
            if found.get('mobile') and found['mobile'] != details.get('mobile'):
                details.update(found)
                return None
            answer, _confidence = classifier.predict(text, among={'yes', 'no', 'cancel'})
            if answer == 'yes':
                slots['allow_duplicate'] = True
                return None
            if answer == 'no':
                raise Finish([say(_('Okay, not added.'),
                                  buttons=[send(_('Add a guest'), 'add guest')])])
            return say(_('Say "yes" to add a new guest with this mobile, type a different '
                         'number, or say "cancel".'))
        mode = 'whole' if key == 'full_name' else 'marker'
        found = _clean(entities.extract(text, name=mode))
        details.update(found)
        if key in found:
            return None
        if key == 'full_name':
            return say(_('Please type the guest\'s name, for example "Ravi Kumar".'),
                       buttons=[cancel_button()])
        return say(_('I could not find a mobile number in that. Please type it like '
                     '0501234567 or +971 50 123 4567.'), buttons=[cancel_button()])

    def change(self, ctx, slots, text):
        found = _clean(entities.extract(text, name='marker'))
        if not found:
            return False
        if found.get('mobile') and found['mobile'] != slots['guest'].get('mobile'):
            slots.pop('allow_duplicate', None)
        slots['guest'].update(found)
        return True

    def summary(self, ctx, slots):
        labels, details = _labels(), slots['guest']
        gender = dict(Guest._meta.get_field('gender').choices)
        rows = [(labels[f], gender.get(details[f], details[f]) if f == 'gender' else details[f])
                for f in FIELDS if details.get(f)]
        return say(_('Please check the details. To change something, type it, for example '
                     '"mobile is 0509876543".'), rows=rows,
                   buttons=confirm_buttons(_('Yes, add guest')))

    def run(self, ctx, slots):
        details = slots['guest']
        # The same form as the New guest page, so the same checks apply.
        form = GuestForm(data={f: details.get(f, '') for f in FIELDS})
        if not form.is_valid():
            raise NotDone([say(_('I could not save that: %(problems)s Type a correction or say '
                                 '"cancel".') % {'problems': form_errors(form)})])
        guest = form.save(commit=False)
        guest.tenant = ctx.tenant
        guest.save()
        audit.log(AuditLog.ACTION_CREATE, guest, actor=ctx.user, request=ctx.request,
                  summary=_('Guest %(name)s saved') % {'name': guest.full_name})
        from .common import remember
        remember(ctx, guest=guest)
        buttons = [link(_('Open guest'), reverse('guests:guest_detail', args=[guest.pk]))]
        if ctx.allowed(MODULE_GUESTS, 'create'):
            buttons.append(start(_('Check in now'), 'check_in', guest=guest.pk))
        buttons.append(send(_('Add another'), 'add guest'))
        return [say(_('Done! %(name)s has been added.') % {'name': guest.full_name},
                    buttons=buttons)]


def form_errors(form):
    problems = []
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else ''
        text = ' '.join(str(e) for e in errors)
        problems.append(f'{label}: {text}' if label else text)
    return ' '.join(problems)
