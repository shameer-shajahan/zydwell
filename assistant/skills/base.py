"""What every skill shares: the request context, messages and buttons.

A skill is one thing the assistant can do ("add a guest", "who has not
paid"). The conversation engine in ``dialog.py`` drives it:

1. ``start(ctx, text)`` reads the first message into ``slots``.
2. ``ask(ctx, slots)`` returns ``(key, message)`` for the next thing it needs
   — a missing detail, or "which Ravi?" — or ``None`` when it has it all.
3. ``answer(ctx, slots, key, text)`` reads the reply to that question;
   ``pick(ctx, slots, key, value)`` takes a button press instead.
4. A skill that changes data (``confirm = True``) shows ``summary`` and waits
   for yes; ``change(ctx, slots, text)`` takes corrections meanwhile. Then
   ``run(ctx, slots)`` saves it. A question skill runs straight away.

``slots`` lives in the session between messages, so it holds only plain
values: ids, strings, ISO dates.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import FeatureFlag


class Finish(Exception):
    """End the conversation now with these messages (cancelled, refused...)."""

    def __init__(self, messages):
        super().__init__('finish')
        self.messages = messages


class Handoff(Exception):
    """This message is really for another skill: start that one with it."""

    def __init__(self, skill):
        super().__init__(skill)
        self.skill = skill


class NotDone(Exception):
    """Stay at the summary and show these messages (a save failed its checks)."""

    def __init__(self, messages):
        super().__init__('not done')
        self.messages = messages


class Ctx:
    def __init__(self, request):
        self.request = request
        self.user = request.user
        self.tenant = request.tenant
        self.today = timezone.localdate()
        self._flags = None

    def allowed(self, module, action='view'):
        """The module is switched on for this account and the user holds the right."""
        if self._flags is None:
            self._flags = dict(FeatureFlag.objects.filter(tenant=self.tenant).values_list(
                'module', 'enabled'))
        return self._flags.get(module, True) and self.user.can(module, action)

    def money(self, value):
        try:
            value = Decimal(value)
        except (InvalidOperation, TypeError):
            return str(value)
        return f'{self.tenant.currency} {value:,.2f}'


# ---------------------------------------------------------------- messages
def say(text, *, rows=None, buttons=None):
    message = {'text': str(text)}
    if rows:
        message['rows'] = [[str(label), str(value)] for label, value in rows]
    if buttons:
        message['buttons'] = [b for b in buttons if b]
    return message


def send(label, text):
    """A button that sends ``text`` as if typed."""
    return {'label': str(label), 'send': text}


def link(label, href):
    return {'label': str(label), 'href': href}


def choose(label, key, value):
    """A button that answers the open question ``key`` with ``value``."""
    return {'label': str(label), 'send': f'@pick:{key}:{value}'}


def start(label, skill, **preset):
    """A button that starts another skill with some details already known."""
    values = ','.join(f'{k}={v}' for k, v in preset.items())
    return {'label': str(label), 'send': f'@start:{skill}:{values}'}


def cancel_button():
    return send(_('Cancel'), 'cancel')


def confirm_buttons(label):
    return [send(label, 'yes'), cancel_button()]


def day(value):
    """A date for people: 25 Nov 2026."""
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return value.strftime('%d %b %Y') if value else ''


def as_date(value):
    return date.fromisoformat(value) if isinstance(value, str) and value else value


def as_decimal(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


class Skill:
    name = ''
    module = ''
    action = 'view'
    confirm = True
    #: The button label when the assistant asks "did you mean…?".
    title = ''
    #: Shown by "help" to people who may use it.
    example = ''

    def start(self, ctx, text):
        return {}

    def preset(self, ctx, values):
        """Slots from a button that starts this skill (``@start:skill:k=v``)."""
        return dict(values)

    def ask(self, ctx, slots):
        return None

    def answer(self, ctx, slots, key, text):
        """Read a typed reply to question ``key``. Return a message to ask again."""
        return None

    def pick(self, ctx, slots, key, value):
        slots[key] = value

    def change(self, ctx, slots, text):
        """Corrections typed at the summary; True when something changed."""
        return False

    def summary(self, ctx, slots):
        raise NotImplementedError

    def run(self, ctx, slots):
        raise NotImplementedError
