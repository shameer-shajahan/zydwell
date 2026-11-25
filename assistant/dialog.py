"""The assistant's side of the conversation.

``reply(request, text)`` takes what the user typed and returns the messages to
show back, each ``{'text', 'rows'?, 'buttons'?}``. A button either sends a
reply (``{'label', 'send'}``) or opens a page (``{'label', 'href'}``).

Where we are in a conversation lives in the session, so it survives a page
reload and needs no table:

    {'flow': 'add_guest', 'slots': {...}, 'awaiting': 'mobile'}

Nothing is saved until the user has seen every detail and said yes.
"""
import re

from django.urls import reverse
from django.utils.translation import gettext as _

from core import audit
from core.constants import MODULE_GUESTS
from core.models import AuditLog
from guests.forms import GuestForm
from guests.models import Guest

from .brain import classifier, entities

SESSION_KEY = 'assistant'
MAX_LENGTH = 500

#: Asked for, in this order, before a guest can be saved.
REQUIRED = ['full_name', 'mobile']
#: Everything the assistant can fill in, in the order the summary shows it.
FIELDS = ['full_name', 'mobile', 'nationality', 'gender', 'email', 'employer']
LIMITS = {'full_name': 150, 'mobile': 32, 'nationality': 80, 'email': 254, 'employer': 150}


# ---------------------------------------------------------------- messages
def _say(text, *, rows=None, buttons=None):
    message = {'text': str(text)}
    if rows:
        message['rows'] = [[str(label), str(value)] for label, value in rows]
    if buttons:
        message['buttons'] = [b for b in buttons if b]
    return message


def _send(label, send):
    return {'label': str(label), 'send': send}


def _link(label, href):
    return {'label': str(label), 'href': href}


def _start_buttons():
    return [_send(_('Add a guest'), 'add guest'), _send(_('What can you do?'), 'help')]


def _greeting():
    return _say(
        _('Hi! I can add a guest for you. Type it the way you would say it, for example: '
          '"add guest Ravi Kumar 0501234567 Indian".'),
        buttons=_start_buttons())


def _help():
    return _say(_(
        'I can add a guest. Try one of these:\n'
        '• add guest Ravi Kumar 0501234567\n'
        '• new tenant from India, name is Maria, mobile +971 55 111 2222\n'
        '• register a guest who works at Emaar\n'
        'I ask for anything missing and show you the details before saving. '
        'Say "cancel" at any time to stop.'),
        buttons=[_send(_('Add a guest'), 'add guest')])


def _labels():
    return {
        'full_name': _('Name'), 'mobile': _('Mobile'), 'nationality': _('Nationality'),
        'gender': _('Gender'), 'email': _('Email'), 'employer': _('Employer'),
    }


def _display(field, value):
    if field == 'gender':
        return dict(Guest._meta.get_field('gender').choices).get(value, value)
    return value


def _summary(slots):
    labels = _labels()
    rows = [(labels[f], _display(f, slots[f])) for f in FIELDS if slots.get(f)]
    return _say(
        _('Please check the details. To change something, type it, for example '
          '"mobile is 0509876543".'),
        rows=rows,
        buttons=[_send(_('Yes, add guest'), 'yes'), _send(_('Cancel'), 'cancel')])


def _question(field, slots, again=False):
    cancel = [_send(_('Cancel'), 'cancel')]
    if field == 'full_name':
        if again:
            return _say(_('Please type the guest\'s name, for example "Ravi Kumar".'),
                        buttons=cancel)
        return _say(_("What is the guest's full name?"), buttons=cancel)
    if again:
        return _say(_('I could not find a mobile number in that. Please type it like '
                      '0501234567 or +971 50 123 4567.'), buttons=cancel)
    return _say(_("What is %(name)s's mobile number?") % {'name': slots['full_name']},
                buttons=cancel)


# ---------------------------------------------------------------- state
def _state(request):
    return dict(request.session.get(SESSION_KEY) or {})


def _keep(request, state):
    request.session[SESSION_KEY] = state


def _clear(request):
    request.session.pop(SESSION_KEY, None)


def _clean(found):
    """Only fields we know, trimmed to what the database takes."""
    return {f: str(v).strip()[:LIMITS.get(f, 150)] for f, v in found.items()
            if f in FIELDS and str(v).strip()}


def _duplicate(request, mobile):
    """A guest in this account who already has this mobile, written any way."""
    tail = re.sub(r'\D', '', mobile)[-4:]
    if not tail:
        return None
    rows = Guest.objects.filter(
        tenant=request.tenant, is_company=False, mobile__contains=tail,
    ).only('pk', 'full_name', 'mobile')
    return next((g for g in rows if entities.same_mobile(g.mobile, mobile)), None)


# ---------------------------------------------------------------- the API
def opening(request):
    """What the chat page shows when it opens: a greeting, or where we left off."""
    state = _state(request)
    if state.get('flow') == 'add_guest':
        return [_say(_('Welcome back. We were adding a guest.'))] + _next(request, state)
    return [_greeting()]


def reply(request, text):
    text = (text or '').strip()[:MAX_LENGTH]
    if not text:
        return [_say(_('Type a message, for example "add a guest".'))]
    state = _state(request)
    if state.get('flow') == 'add_guest':
        return _continue(request, state, text)
    return _fresh(request, text)


def _fresh(request, text):
    intent, _confidence = classifier.predict(text)
    if intent == 'add_guest':
        return _start_add_guest(request, text)
    if intent == 'help':
        return [_help()]
    if intent == 'greet':
        return [_greeting()]
    if intent == 'thanks':
        return [_say(_('You are welcome!'), buttons=[_send(_('Add a guest'), 'add guest')])]
    if intent in ('yes', 'no', 'cancel'):
        return [_say(_('Nothing is in progress right now.'), buttons=_start_buttons())]
    if intent == 'other':
        return [_say(_("I can't do that yet. For now I can add guests."),
                     buttons=_start_buttons())]
    return [_say(_('Sorry, I did not understand that. I can add guests. Try '
                   '"add guest Ravi 0501234567".'), buttons=_start_buttons())]


# ---------------------------------------------------------------- add a guest
def _start_add_guest(request, text):
    if not request.user.can(MODULE_GUESTS, 'create'):
        return [_say(_('You do not have permission to add guests. Ask your admin for access.'))]
    slots = _clean(entities.extract(text, name='guess'))
    state = {'flow': 'add_guest', 'slots': slots, 'awaiting': None}
    lead = _say(_('Got it.') if slots else _("Sure, let's add a guest."))
    return [lead] + _next(request, state)


def _next(request, state):
    """Ask for the next missing detail, warn about a duplicate, or show the summary."""
    slots = state['slots']
    for field in REQUIRED:
        if not slots.get(field):
            state['awaiting'] = field
            _keep(request, state)
            return [_question(field, slots)]

    if not state.get('allow_duplicate'):
        existing = _duplicate(request, slots['mobile'])
        if existing:
            state['awaiting'] = 'duplicate'
            _keep(request, state)
            return [_say(
                _('%(name)s already has the mobile %(mobile)s. Add a new guest anyway?') % {
                    'name': existing.full_name, 'mobile': existing.mobile},
                buttons=[
                    _send(_('Yes, add new guest'), 'yes'),
                    _link(_('Open %(name)s') % {'name': existing.full_name},
                          reverse('guests:guest_detail', args=[existing.pk])),
                    _send(_('Cancel'), 'cancel'),
                ])]

    state['awaiting'] = 'confirm'
    _keep(request, state)
    return [_summary(slots)]


def _continue(request, state, text):
    awaiting = state.get('awaiting')
    intent, _confidence = classifier.predict(text)
    if intent == 'cancel':
        _clear(request)
        return [_say(_('Okay, cancelled. Nothing was saved.'),
                     buttons=[_send(_('Add a guest'), 'add guest')])]
    if intent == 'help':
        return [_help()] + _next(request, state)

    slots = state['slots']
    if awaiting in REQUIRED:
        # Typing the whole thing again ("add guest ravi 050...") is read like a
        # first message; otherwise the reply is the answer to the question.
        if intent == 'add_guest':
            mode = 'guess'
        else:
            mode = 'whole' if awaiting == 'full_name' else 'marker'
        found = _clean(entities.extract(text, name=mode))
        slots.update(found)
        if awaiting not in found:
            _keep(request, state)
            return [_question(awaiting, slots, again=True)]
        return _next(request, state)

    # A summary or a duplicate warning is waiting for an answer. A message
    # that carries a detail ("mobile is 0509...") changes it.
    found = _clean(entities.extract(text, name='marker'))
    if found:
        if found.get('mobile') and found['mobile'] != slots.get('mobile'):
            state.pop('allow_duplicate', None)
        slots.update(found)
        return [_say(_('Updated.'))] + _next(request, state)

    answer, _confidence = classifier.predict(text, among={'yes', 'no', 'cancel'})
    if awaiting == 'duplicate':
        if answer == 'yes':
            state['allow_duplicate'] = True
            return _next(request, state)
        if answer == 'no':
            _clear(request)
            return [_say(_('Okay, not added.'), buttons=[_send(_('Add a guest'), 'add guest')])]
        return [_say(_('Say "yes" to add a new guest with this mobile, type a different '
                       'number, or say "cancel".'))]

    if answer == 'yes':
        return _save(request, state)
    if answer == 'no':
        return [_say(_('No problem. Tell me what to change, for example "name is Ravi Kumar" '
                       'or "mobile is 0509876543". Or say "cancel".'))]
    return [_say(_('Say "yes" to add the guest, type a change, or say "cancel".'),
                 buttons=[_send(_('Yes, add guest'), 'yes'), _send(_('Cancel'), 'cancel')])]


def _save(request, state):
    if not request.user.can(MODULE_GUESTS, 'create'):
        _clear(request)
        return [_say(_('You do not have permission to add guests. Ask your admin for access.'))]
    slots = state['slots']
    # The same form as the New guest page, so the same checks apply.
    form = GuestForm(data={f: slots.get(f, '') for f in FIELDS})
    if not form.is_valid():
        problems = []
        for field, errors in form.errors.items():
            label = form.fields[field].label if field in form.fields else ''
            problems.append(f'{label}: {" ".join(errors)}' if label else ' '.join(errors))
        return [_say(_('I could not save that: %(problems)s Type a correction or say "cancel".')
                     % {'problems': ' '.join(problems)})]

    guest = form.save(commit=False)
    guest.tenant = request.tenant
    guest.save()
    audit.log(AuditLog.ACTION_CREATE, guest, actor=request.user, request=request,
              summary=_('Guest %(name)s saved') % {'name': guest.full_name})
    _clear(request)
    return [_say(
        _('Done! %(name)s has been added.') % {'name': guest.full_name},
        buttons=[
            _link(_('Open guest'), reverse('guests:guest_detail', args=[guest.pk])),
            _link(_('Check in to a bed'), reverse('guests:check_in') + f'?guest={guest.pk}'),
            _send(_('Add another'), 'add guest'),
        ])]
