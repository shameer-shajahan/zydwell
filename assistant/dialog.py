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
from .skills.base import Ctx, Finish, Handoff, NotDone, say, send

logger = logging.getLogger(__name__)

SESSION_KEY = 'assistant'
MAX_LENGTH = 500
#: How sure the model must be before a new request replaces one in progress.
SWITCH_CONFIDENCE = 0.9
#: Questions answered with a name, where any words would do.
FREE_TEXT = {'tenancy', 'guest'}

_PICK = re.compile(r'^@pick:(\w+):(.{0,80})$')
_START = re.compile(r'^@start:(\w+):(.{0,200})$')
_TEACH = re.compile(r'^@teach:(\w+)$')
#: The last sentence the assistant did not understand, waiting for "I meant…".
UNCLEAR_KEY = 'assistant_unclear'


# ---------------------------------------------------------------- state
def _state(request):
    return dict(request.session.get(SESSION_KEY) or {})


def _keep(request, state):
    request.session[SESSION_KEY] = state


def _clear(request):
    request.session.pop(SESSION_KEY, None)


def _predict(ctx, text, among=None):
    """The intent, from this account's model (the dataset plus what it was taught)."""
    if not hasattr(ctx, 'model'):
        ctx.model = classifier.model_for(ctx.tenant)
    return classifier.predict(text, among=among, model=ctx.model)


# ---------------------------------------------------------------- small talk
def _usable(ctx):
    return [s for s in SKILLS.values() if not s.module or ctx.allowed(s.module, s.action)]


def _start_buttons(ctx):
    buttons = []
    if 'add_guest' in {s.name for s in _usable(ctx)}:
        buttons.append(send(_('Add a guest'), 'add guest'))
    buttons.append(send(_('What can you do?'), 'help'))
    return buttons


def _hello(ctx):
    """Good morning / afternoon / evening, with the person's first name."""
    from django.utils import timezone

    hour = timezone.localtime().hour
    part = (_('Good morning') if hour < 12 else _('Good afternoon') if hour < 17
            else _('Good evening'))
    name = (ctx.user.first_name or ctx.user.get_username() or '').strip()
    return f'{part}, {name}!' if name else f'{part}!'


def _greeting(ctx):
    return say(_hello(ctx) + ' ' + _(
        'Tell me what to do in your own words, for example "Ravi paid 800 cash", '
        '"put Ravi in room 101 bed A" or "who has not paid". Type "help" to see '
        'everything I can do.'), buttons=_start_buttons(ctx))


#: Short chat people expect a person to answer kindly, not with "I did not understand".
SMALL_TALK = [
    (re.compile(r"\b(?:who\s+are\s+(?:you|u)|what\s+are\s+you|your\s+name|who\s+r\s+u|"
                r"are\s+(?:you|u)\s+(?:a\s+)?(?:bot|robot|human|real))\b", re.I),
     lambda ctx: _("I'm the ZyDwell assistant. I can add guests, check them in and out, take "
                   "rent and deposits, record expenses, and tell you about beds, dues and "
                   "money. Just tell me what you need, the way you would tell a colleague.")),
    (re.compile(r"\b(?:how\s+(?:are|r)\s+(?:you|u)|how\s+is\s+it\s+going|how\s+are\s+things|"
                r"what'?s\s+up|wassup|sup)\b", re.I),
     lambda ctx: _("I'm doing well, thanks for asking! What shall we get done?")),
    (re.compile(r"\b(?:good\s*night|bye|goodbye|see\s+(?:you|u)|gn|take\s+care)\b", re.I),
     lambda ctx: _('Good night! I am here whenever you need me.')),
    (re.compile(r"\b(?:good\s+(?:bot|job|work)|well\s+done|you\s+are\s+(?:good|great|smart|"
                r"awesome|the\s+best)|great\s+work|nice\s+work)\b", re.I),
     lambda ctx: _('Thank you, that is kind! Anything else?')),
    (re.compile(r"\b(?:what\s+(?:else|next)|anything\s+else|what\s+now)\b", re.I),
     lambda ctx: _('Here is what I can do:')),
]


def _small_talk(ctx, text):
    if len(classifier.words(text)) > 7:
        return None
    for pattern, answer in SMALL_TALK:
        if pattern.search(text):
            message = answer(ctx)
            if message == _('Here is what I can do:'):
                return [say(message), _help(ctx)]
            return [say(message, buttons=_start_buttons(ctx))]
    return None


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
    taught = _TEACH.match(text)
    if taught:
        return _learn(ctx, taught.group(1))
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
    chat = _small_talk(ctx, text)
    if chat:
        return chat
    intent, _confidence = _predict(ctx, text)
    skill = SKILLS.get(intent)
    if skill is not None:
        return _begin(ctx, skill, text)
    if intent == 'help':
        return [_help(ctx)]
    if intent == 'greet':
        return [_greeting(ctx)]
    if intent == 'thanks':
        return [say(_('You are welcome! Anything else?'), buttons=_start_buttons(ctx))]
    if intent in ('yes', 'no', 'cancel'):
        return [say(_('Nothing is in progress right now.'), buttons=_start_buttons(ctx))]
    if intent == 'other':
        return [say(_("I can't do that yet. Here is what I can do:")), _help(ctx)]
    # A question about the data the model has no name for ("how many guests from
    # india?", "total deposit held"): read it and answer from the data.
    from .skills.data import looks_like_a_question, subject_of
    if looks_like_a_question(text) and subject_of(text):
        return _begin(ctx, SKILLS['data_question'], text)
    # A guest's name and a free bed, with no verb ("akash to 102 a4"): a check-in.
    check_in = SKILLS['check_in']
    if ctx.allowed(check_in.module, check_in.action):
        try:
            slots = check_in.start(ctx, text)
        except Handoff:
            slots = {}
        if slots.get('guest') and (slots.get('bed') or slots.get('place_options')):
            return _begin(ctx, check_in, text)
    # A name? Show that guest — and if there was more than the name
    # ("akash gav notis"), ask what was meant too, so it can be learnt.
    info = SKILLS['guest_info']
    if ctx.allowed(info.module, info.action):
        slots = info.start(ctx, text)
        if slots.get('guest_options') or slots.get('free_place'):
            return _begin(ctx, info, text)
        if slots.get('guest'):
            card = _begin(ctx, info, text)
            if not _words_beyond_name(ctx, text, slots['guest']):
                return card
            return card + _ask_meaning(ctx, text)
    return _ask_meaning(ctx, text)


def _words_beyond_name(ctx, text, guest_pk):
    from . import lookup

    guest = lookup.guest(ctx, guest_pk)
    tokens = [t.lower() for t in re.findall(r"[\w']+", guest.full_name if guest else '')]
    return [w for w in lookup.name_words(text) if lookup._word_score(w, tokens) < 0.85]


def _ask_meaning(ctx, text):
    """Not understood: offer the likeliest jobs, and learn from the one picked."""
    if not hasattr(ctx, 'model'):
        ctx.model = classifier.model_for(ctx.tenant)
    usable = {s.name: s for s in _usable(ctx)}
    ranked = sorted(classifier.scores(ctx.model, text).items(), key=lambda kv: -kv[1])
    guesses = [usable[name] for name, _score in ranked if name in usable][:3]
    if not guesses:
        guesses = list(usable.values())[:3]
    ctx.request.session[UNCLEAR_KEY] = text
    buttons = [send(skill.title, f'@teach:{skill.name}') for skill in guesses]
    buttons.append(send(_('Something else'), 'help'))
    return [say(_('Sorry, I did not understand that. What did you want to do? Pick one and '
                  'I will remember it for next time.'), buttons=buttons)]


def _learn(ctx, name):
    """The person picked what they meant: keep the sentence, then do it."""
    from .lookup import mask_names
    from .models import LearnedPhrase

    text = ctx.request.session.pop(UNCLEAR_KEY, None)
    skill = SKILLS.get(name)
    if not text or skill is None:
        return [say(_('That question is closed. What would you like to do?'),
                    buttons=_start_buttons(ctx))]
    if skill.module and not ctx.allowed(skill.module, skill.action):
        return _refuse(skill)
    phrase = mask_names(ctx, text)
    if classifier.words(phrase):
        LearnedPhrase.objects.update_or_create(
            tenant=ctx.tenant, text=phrase.lower(),
            defaults={'intent': skill.name, 'taught_by': ctx.user})
    _clear(ctx.request)
    return [say(_('Thanks, I will remember that.'))] + _begin(ctx, skill, text)


def _remember(ctx, slots):
    """Whoever this conversation settled on is "he" or "she" in the next one."""
    from . import lookup
    from .skills.common import remember

    if slots.get('tenancy'):
        stay = lookup.stay(ctx, slots['tenancy'])
        if stay is not None:
            remember(ctx, tenancy=stay)
    elif slots.get('guest') and str(slots['guest']).isdigit():
        guest = lookup.guest(ctx, slots['guest'])
        if guest is not None:
            remember(ctx, guest=guest)


def _refuse(skill):
    return [say(_('You do not have permission for that. Ask your admin for access.'))]


def _begin(ctx, skill, text, handed=False):
    if skill.module and not ctx.allowed(skill.module, skill.action):
        return _refuse(skill)
    try:
        slots = skill.start(ctx, text)
    except Handoff as other:
        if handed or other.skill not in SKILLS:
            raise
        return _begin(ctx, SKILLS[other.skill], text, handed=True)
    state = {'skill': skill.name, 'slots': slots, 'awaiting': None}
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
    _remember(ctx, state['slots'])
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

    intent, confidence = _predict(ctx, text)
    if intent == 'cancel':
        _clear(ctx.request)
        return [say(_('Okay, cancelled. Nothing was saved.'), buttons=_start_buttons(ctx))]
    if intent == 'help':
        return [_help(ctx)] + _advance(ctx, skill, state)
    # "hi", "thanks", "who are you" in the middle: answer like a person, then
    # ask the open question again — they are not names or amounts.
    chat = _small_talk(ctx, text)
    if chat is None and intent in ('greet', 'thanks') and confidence >= 0.9:
        chat = [say(_hello(ctx) if intent == 'greet' else _('You are welcome!'))]
    if chat:
        # The reply without its "start something" buttons: we are mid-way.
        replies = [{k: v for k, v in message.items() if k != 'buttons'} for message in chat]
        return replies + [say(_('Back to what we were doing:'))] + _advance(ctx, skill, state)
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
    answer, _confidence = _predict(ctx, text, among={'yes', 'no', 'cancel'})
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
