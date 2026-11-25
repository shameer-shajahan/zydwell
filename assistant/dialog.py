"""The assistant's side of the conversation.

``reply(request, text)`` takes what the user typed and returns the messages to
show back, each ``{'text', 'rows'?, 'buttons'?}``. A button either sends a
reply (``{'label', 'send'}``) or opens a page (``{'label', 'href'}``).

The model (``brain/classifier.py``) says what a message wants; the matching
skill (``skills/``) does it. Where a conversation has got to lives in the
session, so it survives a page reload and needs no table:

    {'skill': 'payment', 'slots': {...}, 'awaiting': 'amount'}

Nothing is changed until the user has seen every detail and said yes.
Buttons answer with tokens the user never types:

    @pick:<question>:<value>      answers the open question
    @start:<skill>:<k>=<v>,...    starts a skill with some details known
"""
import copy
import logging
import re

from django.utils.translation import gettext as _

from .brain import classifier
from .skills import SKILLS
from .skills.base import Ctx, Finish, NotDone, say, send

logger = logging.getLogger(__name__)

SESSION_KEY = 'assistant'
MAX_LENGTH = 500
#: How sure the model must be before a new request replaces one in progress.
SWITCH_CONFIDENCE = 0.9
#: Questions answered with a name, where any words would do.
FREE_TEXT = {'tenancy', 'guest'}

_PICK = re.compile(r'^@pick:(\w+):(.{0,80})$')
_START = re.compile(r'^@start:(\w+):(.{0,200})$')


# ---------------------------------------------------------------- state
def _state(request):
    return dict(request.session.get(SESSION_KEY) or {})


def _keep(request, state):
    request.session[SESSION_KEY] = state


def _clear(request):
    request.session.pop(SESSION_KEY, None)


# ---------------------------------------------------------------- small talk
def _usable(ctx):
    return [s for s in SKILLS.values() if not s.module or ctx.allowed(s.module, s.action)]


def _start_buttons(ctx):
    buttons = []
    if 'add_guest' in {s.name for s in _usable(ctx)}:
        buttons.append(send(_('Add a guest'), 'add guest'))
    buttons.append(send(_('What can you do?'), 'help'))
    return buttons


def _greeting(ctx):
    return say(_('Hi! Tell me what to do in your own words, for example "Ravi paid 800 cash", '
                 '"put Ravi in room 101 bed A" or "who has not paid". Type "help" to see '
                 'everything I can do.'), buttons=_start_buttons(ctx))


def _help(ctx):
    skills = _usable(ctx)
    jobs = [s for s in skills if s.confirm]
    questions = [s for s in skills if not s.confirm]
    lines = []
    if jobs:
        lines.append(_('Jobs I can do (I always show you the details before saving):'))
        lines += [f'• {s.example}' for s in jobs]
    if questions:
        lines.append(_('Questions I can answer:'))
        lines += [f'• {s.example}' for s in questions]
    lines.append(_('Say "cancel" at any time to stop.'))
    return say('\n'.join(lines))


# ---------------------------------------------------------------- the API
def opening(request):
    """What the chat shows when it opens: a greeting, or where we left off."""
    ctx = Ctx(request)
    state = _state(request)
    skill = SKILLS.get(state.get('skill'))
    if skill is not None:
        try:
            return [say(_('Welcome back. We were in the middle of something.'))] + _advance(
                ctx, skill, state)
        except (Finish, NotDone, AttributeError, KeyError, TypeError, ValueError):
            # The guest or bed behind it may be gone since: start afresh.
            _clear(request)
    return [_greeting(ctx)]


def reply(request, text):
    ctx = Ctx(request)
    text = (text or '').strip()[:MAX_LENGTH]
    if not text:
        return [say(_('Type a message, for example "who has not paid".'))]
    started = _START.match(text)
    if started:
        return _begin_preset(ctx, started.group(1), started.group(2))
    state = _state(request)
    skill = SKILLS.get(state.get('skill'))
    if skill is None:
        _clear(request)
        if _PICK.match(text):
            return [say(_('That question is closed. What would you like to do?'),
                        buttons=_start_buttons(ctx))]
        return _fresh(ctx, text)
    try:
        return _continue(ctx, skill, state, text)
    except Finish as done:
        _clear(request)
        return done.messages
    except (AttributeError, KeyError, TypeError, ValueError):
        # A record it relied on changed under it (checked out elsewhere,
        # deleted). Say so and start afresh rather than fail the request.
        logger.exception('Assistant conversation reset')
        _clear(request)
        return [say(_('Something changed while we were talking, so I have stopped. '
                      'Please start again.'), buttons=_start_buttons(ctx))]


def _fresh(ctx, text):
    intent, _confidence = classifier.predict(text)
    skill = SKILLS.get(intent)
    if skill is not None:
        return _begin(ctx, skill, text)
    if intent == 'help':
        return [_help(ctx)]
    if intent == 'greet':
        return [_greeting(ctx)]
    if intent == 'thanks':
        return [say(_('You are welcome!'))]
    if intent in ('yes', 'no', 'cancel'):
        return [say(_('Nothing is in progress right now.'), buttons=_start_buttons(ctx))]
    if intent == 'other':
        return [say(_("I can't do that yet. Here is what I can do:")), _help(ctx)]
    # Just a name? Show that guest.
    info = SKILLS['guest_info']
    if ctx.allowed(info.module, info.action):
        slots = info.start(ctx, text)
        if slots.get('guest') or slots.get('guest_options'):
            return _begin(ctx, info, text)
    return [say(_('Sorry, I did not understand that. Try "Ravi paid 800 cash" or '
                  '"free beds", or type "help".'), buttons=_start_buttons(ctx))]


def _refuse(skill):
    return [say(_('You do not have permission for that. Ask your admin for access.'))]


def _begin(ctx, skill, text):
    if skill.module and not ctx.allowed(skill.module, skill.action):
        return _refuse(skill)
    state = {'skill': skill.name, 'slots': skill.start(ctx, text), 'awaiting': None}
    try:
        return _advance(ctx, skill, state)
    except Finish as done:
        _clear(ctx.request)
        return done.messages


def _begin_preset(ctx, name, raw):
    skill = SKILLS.get(name)
    if skill is None:
        return [say(_('Sorry, I cannot do that.'))]
    if skill.module and not ctx.allowed(skill.module, skill.action):
        return _refuse(skill)
    values = dict(part.split('=', 1) for part in raw.split(',') if '=' in part)
    state = {'skill': skill.name, 'slots': skill.preset(ctx, values), 'awaiting': None}
    try:
        return _advance(ctx, skill, state)
    except Finish as done:
        _clear(ctx.request)
        return done.messages


def _advance(ctx, skill, state):
    """Ask what is still missing, or show the summary, or (a question) answer."""
    question = skill.ask(ctx, state['slots'])
    if question:
        state['awaiting'], message = question
        _keep(ctx.request, state)
        return [message]
    if not skill.confirm:
        _clear(ctx.request)
        return skill.run(ctx, state['slots'])
    state['awaiting'] = 'confirm'
    _keep(ctx.request, state)
    return [skill.summary(ctx, state['slots'])]


def _continue(ctx, skill, state, text):
    slots, awaiting = state['slots'], state.get('awaiting')
    picked = _PICK.match(text)
    if picked:
        skill.pick(ctx, slots, picked.group(1), picked.group(2))
        return _advance(ctx, skill, state)

    intent, confidence = classifier.predict(text)
    if intent == 'cancel':
        _clear(ctx.request)
        return [say(_('Okay, cancelled. Nothing was saved.'), buttons=_start_buttons(ctx))]
    if intent == 'help':
        return [_help(ctx)] + _advance(ctx, skill, state)
    other = SKILLS.get(intent)
    switch = (other is not None and other.name != skill.name
              and confidence >= SWITCH_CONFIDENCE)

    def switch_over():
        # A clear new request: drop the old one rather than read this as an answer.
        _clear(ctx.request)
        return [say(_('Okay, leaving that.'))] + _begin(ctx, other, text)

    if awaiting != 'confirm':
        # "Who?" takes any words as a name, so a clear new request goes first.
        if switch and awaiting in FREE_TEXT:
            return switch_over()
        # Otherwise the message is tried as the answer, on a copy: "rent 900"
        # answers "what rent?" even though it also sounds like a payment.
        trial = copy.deepcopy(slots)
        problem = skill.answer(ctx, trial, awaiting, text)
        if problem and switch:
            return switch_over()
        if problem:
            _keep(ctx.request, state)
            return [problem]
        state['slots'] = trial
        return _advance(ctx, skill, state)

    # The summary is waiting for yes. A message with details changes them.
    if skill.change(ctx, slots, text):
        return [say(_('Updated.'))] + _advance(ctx, skill, state)
    if switch:
        return switch_over()
    answer, _confidence = classifier.predict(text, among={'yes', 'no', 'cancel'})
    if answer == 'yes':
        try:
            messages = skill.run(ctx, slots)
        except NotDone as failed:
            _keep(ctx.request, state)
            return failed.messages
        _clear(ctx.request)
        return messages
    if answer == 'no':
        return [say(_('No problem. Type what to change, or say "cancel".'))]
    return [say(_('Say "yes" to save, type a change, or say "cancel".'),
                buttons=[send(_('Yes'), 'yes'), send(_('Cancel'), 'cancel')])]
