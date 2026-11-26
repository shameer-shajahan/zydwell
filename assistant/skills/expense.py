"""Record an expense or other income: "electricity bill 450 for Sunrise Tower",
"paid plumber 150", "received 100 for laundry"."""
import re

from django.urls import reverse
from django.utils.translation import gettext as _

from accounting.forms import LedgerEntryForm
from accounting.models import KIND_EXPENSE, KIND_INCOME, Category
from core import audit
from core.constants import MODULE_ACCOUNTING
from core.models import AuditLog

from .. import lookup
from .add_guest import form_errors
from .base import (NotDone, Skill, as_date, as_decimal, cancel_button, choose, confirm_buttons,
                   day, link, say)
from .common import read

_PAYEE = re.compile(r'\b(?:paid\s+to|given\s+to|payee)\s+([A-Za-z][\w&.\' -]{1,60}?)(?=\s+(?:for|on|by|'
                    r'in|at|of)\b|[\d,.;]|$)', re.I)
_CATEGORY = re.compile(r'\b(?:category|under|as)\s+([a-z][a-z &-]{2,40})', re.I)


class Expense(Skill):
    name = 'expense'
    module = MODULE_ACCOUNTING
    action = 'create'
    title = _('Add an expense')
    example = _('electricity bill 450 for Sunrise Tower')
    kind = KIND_EXPENSE

    def start(self, ctx, text):
        slots = {'note': (text or '').strip()[:200]}
        self._take(ctx, slots, read(ctx, text, forward=False, place=True))
        if not slots.get('building'):
            mine = list(lookup.buildings(ctx)[:2])
            if len(mine) == 1:
                slots['building'] = mine[0].pk
        return slots

    def _take(self, ctx, slots, found):
        changed = False
        if found['amounts']:
            slots['amount'] = str(found['amounts'][0])
            changed = True
        text = found['text']
        named = _CATEGORY.search(text)
        category = lookup.find_category(ctx, named.group(1) if named else text, kind=self.kind)
        if category is not None:
            slots['category'] = category.pk
            changed = True
        if found.get('building') is not None:
            slots['building'] = found['building'].pk
            slots.pop('room', None)
            changed = True
        if found.get('room'):
            building = lookup.building(ctx, slots['building']) if slots.get('building') else None
            rooms = lookup.find_rooms(ctx, found['room'], building)
            if len(rooms) == 1:
                slots['room'] = rooms[0].pk
                slots['building'] = rooms[0].apartment.floor.building_id
                changed = True
        if found.get('date'):
            slots['date'] = found['date'].isoformat()
            changed = True
        payee = _PAYEE.search(text)
        if payee and not lookup.find_building(ctx, payee.group(1))[0]:
            slots['payee'] = payee.group(1).strip()[:120]
            changed = True
        return changed

    def ask(self, ctx, slots):
        if not as_decimal(slots.get('amount')):
            return 'amount', say(_('How much was it?'), buttons=[cancel_button()])
        if not slots.get('building') or not lookup.building(ctx, slots['building']):
            slots.pop('building', None)
            buildings = list(lookup.buildings(ctx)[:8])
            return 'building', say(_('Which building is it for?'),
                                   buttons=[choose(b.name, 'building', b.pk) for b in buildings]
                                   + [cancel_button()])
        if not slots.get('category'):
            other = lookup.fallback_category(ctx, self.kind)
            if other is not None:
                slots['category'] = other.pk
        return None

    def answer(self, ctx, slots, key, text):
        found = read(ctx, text, forward=False, place=True)
        if key == 'amount' and not found['amounts']:
            return say(_('Type the amount as a number, like 450.'), buttons=[cancel_button()])
        if key == 'building' and found.get('building') is None and not found.get('room'):
            return say(_('Pick the building from the list.'))
        self._take(ctx, slots, found)
        return None

    def pick(self, ctx, slots, key, value):
        if key == 'building' and str(value).isdigit() and lookup.building(ctx, int(value)):
            slots['building'] = int(value)

    def change(self, ctx, slots, text):
        return self._take(ctx, slots, read(ctx, text, forward=False, place=True))

    def summary(self, ctx, slots):
        category = lookup.category(ctx, slots['category'], kind=self.kind)
        place = lookup.building(ctx, slots['building']).name
        if slots.get('room'):
            room = lookup.room(ctx, slots['room'])
            place = lookup.room_label(room) if room else place
        rows = [(_('Amount'), ctx.money(slots['amount'])),
                (_('Category'), category.name if category else '—'),
                (_('For'), place),
                (_('Date'), day(as_date(slots.get('date')) or ctx.today)),
                (_('Note'), slots.get('note', ''))]
        if slots.get('payee'):
            rows.append(((_('Paid to') if self.kind == KIND_EXPENSE else _('From')), slots['payee']))
        others = Category.objects.filter(tenant=ctx.tenant, kind=self.kind, is_active=True,
                                         is_system=False)
        example = (others.exclude(pk=slots['category']).first() or category).name.lower()
        what = _('expense') if self.kind == KIND_EXPENSE else _('income')
        return say(_('Please check the %(what)s. To change the category, type it, for example '
                     '"category %(example)s".') % {'what': what,
                                                   'example': re.split(r'\W+', example)[0]},
            rows=rows, buttons=confirm_buttons(_('Yes, save')))

    def run(self, ctx, slots):
        form = LedgerEntryForm(data={
            'kind': self.kind, 'category': slots['category'], 'amount': slots['amount'],
            'entry_date': (as_date(slots.get('date')) or ctx.today).isoformat(),
            'description': slots.get('note', ''), 'building': slots['building'], 'floor': '',
            'apartment': '', 'room': slots.get('room') or '', 'payee': slots.get('payee', ''),
            'reference': '',
        }, tenant=ctx.tenant, user=ctx.user)
        if not form.is_valid():
            raise NotDone([say(_('I could not save that: %(problems)s Type a correction or say '
                                 '"cancel".') % {'problems': form_errors(form)})])
        entry = form.save(commit=False)
        entry.tenant = ctx.tenant
        entry.recorded_by = ctx.user
        entry.save()
        audit.log(AuditLog.ACTION_CREATE, entry, actor=ctx.user, request=ctx.request,
                  summary=_('%(kind)s %(amount)s — %(cat)s') % {
                      'kind': entry.get_kind_display(), 'amount': entry.amount,
                      'cat': entry.category.name})
        return [say(_('Saved: %(amount)s for %(cat)s.') % {
            'amount': ctx.money(entry.amount), 'cat': entry.category.name},
            buttons=[link(_('Open income & expense'), reverse('accounting:ledger'))])]


class Income(Expense):
    """Money in that is not rent or a deposit: laundry, a key card, a fine."""

    name = 'income'
    title = _('Add other income')
    example = _('received 100 for laundry')
    kind = KIND_INCOME
