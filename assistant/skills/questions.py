"""Questions: "where is Ravi", "free beds in Sunrise", "who has not paid", "today's collection".

Each answers at once, from the same data the matching screen shows the user
— their buildings, their modules — and offers the obvious next step.
"""
from collections import OrderedDict
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse
from django.utils.translation import gettext as _

from accounting.models import KIND_EXPENSE, LedgerEntry, totals
from billing import services as billing
from billing.models import Invoice, Payment
from core.constants import MODULE_ACCOUNTING, MODULE_BILLING, MODULE_GUESTS, MODULE_PROPERTIES
from properties.models import occupancy_for, occupancy_grouped

from .. import lookup
from ..brain import parse
from .base import Skill, day, link, say, start
from .common import answer_guest, ask_guest, pick_guest, read, stay_place, take_guests

LIST_LIMIT = 10


def _place_slots(ctx, text, place=False):
    """The building (and room) a question names, and any place it could not find."""
    found = read(ctx, text, place=place, building=True)
    slots = {}
    if found.get('building') is not None:
        slots['building'] = found['building'].pk
    else:
        words = [w for w in lookup.name_words(found['rest'])
                 if w not in parse.MONTHS and w not in parse.WEEKDAYS]
        if words:
            slots['unknown_place'] = ' '.join(words)
    return slots, found


def _not_found(slots):
    if not slots.get('unknown_place'):
        return ''
    return _('I could not find a building called "%(name)s".') % {
        'name': slots['unknown_place']} + ' '


class GuestInfo(Skill):
    name = 'guest_info'
    module = MODULE_GUESTS
    confirm = False
    title = _('Guest details')
    example = _('where is Ravi / Ravi balance')

    def start(self, ctx, text):
        slots = {}
        take_guests(ctx, slots, read(ctx, text, building=True))
        return slots

    def preset(self, ctx, values):
        slots = {}
        if str(values.get('guest', '')).isdigit():
            pick_guest(ctx, slots, values['guest'])
        return slots

    def ask(self, ctx, slots):
        return ask_guest(ctx, slots, _('Which guest? Type their name or mobile number.'))

    def answer(self, ctx, slots, key, text):
        return answer_guest(ctx, slots, text)

    def pick(self, ctx, slots, key, value):
        pick_guest(ctx, slots, value)

    def run(self, ctx, slots):
        guest = lookup.guest(ctx, slots['guest'])
        stay = lookup.stay_of(ctx, guest)
        rows = [(_('Mobile'), guest.mobile)]
        if guest.nationality:
            rows.append((_('Nationality'), guest.nationality))
        if stay:
            rows += [(_('Staying at'), stay.location_label()),
                     (_('Since'), day(stay.check_in_date)),
                     (_('Rent'), ctx.money(stay.monthly_rent))]
            if stay.status and stay.status.code == 'notice':
                rows.append((_('Leaving on'), day(stay.expected_check_out) or _('not set')))
        else:
            rows.append((_('Staying at'), _('Not staying now') if not ctx.user.is_staff_member
                         else _('Not staying in your buildings')))
        can_bill = ctx.allowed(MODULE_BILLING)
        owed = guest.outstanding_balance() if can_bill else None
        if can_bill:
            rows.append((_('Owes'), ctx.money(owed)))
            credit = guest.advance_credit()
            if credit > 0:
                rows.append((_('Advance credit'), ctx.money(credit)))
        if stay:
            rows.append((_('Deposit held'), ctx.money(stay.deposit_held())))
        buttons = [link(_('Open guest'), reverse('guests:guest_detail', args=[guest.pk]))]
        if stay and owed and owed > 0 and ctx.allowed(MODULE_BILLING, 'create'):
            buttons.append(start(_('Collect rent'), 'payment', tenancy=stay.pk))
        if stay and ctx.allowed(MODULE_GUESTS, 'edit') and not stay.is_team_let:
            if not (stay.status and stay.status.code == 'notice'):
                buttons.append(start(_('Give notice'), 'give_notice', tenancy=stay.pk))
            buttons.append(start(_('Check out'), 'check_out', tenancy=stay.pk))
            buttons.append(start(_('Change check-in date'), 'edit_check_in', tenancy=stay.pk))
        if not stay and ctx.allowed(MODULE_GUESTS, 'create'):
            buttons.append(start(_('Check in'), 'check_in', guest=guest.pk))
        return [say(guest.full_name, rows=rows, buttons=buttons)]


class Beds(Skill):
    """Free beds and occupancy: one skill, two questions."""

    module = MODULE_PROPERTIES
    confirm = False

    def __init__(self, name):
        self.name = name
        self.title = _('Free beds') if name == 'free_beds' else _('Occupancy')
        self.example = (_('free beds in Sunrise Tower') if name == 'free_beds'
                        else _('how many beds are occupied'))

    def start(self, ctx, text):
        slots, found = _place_slots(ctx, text, place=True)
        if found.get('room'):
            rooms = lookup.find_rooms(ctx, found['room'], found.get('building'))
            if len(rooms) == 1:
                slots['room'] = rooms[0].pk
        return slots

    def run(self, ctx, slots):
        building = lookup.building(ctx, slots['building']) if slots.get('building') else None
        room = lookup.room(ctx, slots['room']) if slots.get('room') else None
        beds = lookup.beds(ctx)
        place = _('All your buildings')
        if room is not None:
            beds, place = beds.filter(room=room), _('room %(room)s') % {'room': room.number}
        elif building is not None:
            beds, place = beds.filter(room__apartment__floor__building=building), building.name
        occ = occupancy_for(beds)
        text = _not_found(slots) + _('%(place)s: %(occupied)s of %(total)s beds taken '
                                     '(%(percent)s%%), %(free)s free.') % {
            'place': place, 'occupied': occ['occupied'], 'total': occ['total'],
            'percent': occ['percent'], 'free': occ['available']}
        rows, buttons = [], []
        if self.name == 'occupancy':
            if building is None and room is None:
                by_building = occupancy_grouped(beds, 'room__apartment__floor__building')
                for bldg in lookup.buildings(ctx):
                    numbers = by_building.get(bldg.pk)
                    if numbers:
                        rows.append((bldg.name,
                                     _('%(occupied)s / %(total)s · %(percent)s%%') % numbers))
        else:
            free = list(lookup.free_beds(ctx, building=building, room=room)[:LIST_LIMIT])
            for bed in free:
                rows.append((lookup.bed_label(bed), ctx.money(bed.monthly_price)
                             if bed.monthly_price else _('price not set')))
            if occ['available'] > len(free):
                text += ' ' + _('Showing the first %(count)s.') % {'count': len(free)}
            if ctx.allowed(MODULE_GUESTS, 'create'):
                buttons += [start(_('Check in to %(bed)s') % {'bed': str(bed)}, 'check_in', bed=bed.pk)
                            for bed in free[:3]]
        buttons.append(link(_('Open bed board'), reverse('properties:bed_board')))
        return [say(text, rows=rows, buttons=buttons)]


def _scoped_invoices(ctx):
    qs = Invoice.objects.filter(tenant=ctx.tenant).select_related(
        'tenancy__guest', 'tenancy__room', 'tenancy__bed', 'tenancy__apartment', 'status')
    return ctx.user.scope_queryset(qs, building_field='tenancy__building',
                                   floor_field='tenancy__floor', apartment_field='tenancy__apartment')


class Overdue(Skill):
    name = 'overdue'
    module = MODULE_BILLING
    confirm = False
    title = _('Who has not paid')
    example = _('who has not paid')

    def start(self, ctx, text):
        slots, _found = _place_slots(ctx, text)
        slots['this_month'] = parse.find_period(text, ctx.today)[2] == 'this month'
        return slots

    def run(self, ctx, slots):
        invoices = _scoped_invoices(ctx)
        building = lookup.building(ctx, slots['building']) if slots.get('building') else None
        if building is not None:
            invoices = invoices.filter(tenancy__building=building)
        owing = billing.owing(invoices)
        if slots.get('this_month'):
            start_of_month = ctx.today.replace(day=1)
            owing = owing.filter(period_start__gte=start_of_month, period_start__lte=ctx.today)
            heading = _('Unpaid rent for this month')
        else:
            owing = owing.filter(due_date__lt=ctx.today)
            heading = _('Overdue rent')
        people = OrderedDict()
        for invoice in owing:
            key = invoice.tenancy_id
            row = people.setdefault(key, {'tenancy': invoice.tenancy, 'amount': Decimal('0'),
                                          'late': 0})
            row['amount'] += invoice.balance
            row['late'] = max(row['late'], invoice.days_overdue or 0)
        if not people:
            return [say(_not_found(slots) + _('%(heading)s: nobody owes anything.') % {
                'heading': heading})]
        ranked = sorted(people.values(), key=lambda r: (-r['late'], -r['amount']))
        total = sum((r['amount'] for r in ranked), Decimal('0'))
        rows = []
        for row in ranked[:LIST_LIMIT]:
            stay = row['tenancy']
            label = f'{stay.guest.full_name} · {stay_place(stay)}'
            late = (_('%(days)s days late') % {'days': row['late']} if row['late']
                    else _('not due yet'))
            rows.append((label, f"{ctx.money(row['amount'])} · {late}"))
        text = _not_found(slots) + _('%(heading)s%(place)s: %(count)s guests owe %(total)s.') % {
            'heading': heading, 'place': f' · {building.name}' if building else '',
            'count': len(ranked), 'total': ctx.money(total)}
        if len(ranked) > LIST_LIMIT:
            text += ' ' + _('Showing the %(count)s most overdue.') % {'count': LIST_LIMIT}
        buttons = []
        if ctx.allowed(MODULE_BILLING, 'create'):
            buttons += [start(_('Collect from %(name)s') % {'name': r['tenancy'].guest.full_name},
                              'payment', tenancy=r['tenancy'].pk) for r in ranked[:3]]
        buttons.append(link(_('Open overdue list'), reverse('billing:overdue')))
        return [say(text, rows=rows, buttons=buttons)]


class Money(Skill):
    """Collections, income, expenses and profit over a period."""

    name = 'money'
    module = ''          # payments or the ledger: checked per question in run()
    confirm = False
    title = _('Money totals')
    example = _("today's collection / expenses this month")

    def start(self, ctx, text):
        low = (text or '').lower()
        if any(w in low for w in ('profit', 'loss', 'net ')):
            measure = 'profit'
        elif any(w in low for w in ('expense', 'spent', 'spend', 'cost', 'expence')):
            measure = 'expense'
        elif any(w in low for w in ('income', 'earn', 'revenue')):
            measure = 'income'
        else:
            measure = 'collection'
        start_day, end_day, label = parse.find_period(text, ctx.today)
        if start_day is None:
            if measure == 'collection':
                start_day, end_day, label = ctx.today, ctx.today, 'today'
            else:
                start_day, end_day, label = ctx.today.replace(day=1), ctx.today, 'this month'
        slots, _found = _place_slots(ctx, text)
        slots.update({'measure': measure, 'start': start_day.isoformat(),
                      'end': end_day.isoformat(), 'label': label})
        return slots

    def run(self, ctx, slots):
        from datetime import date

        start_day, end_day = date.fromisoformat(slots['start']), date.fromisoformat(slots['end'])
        building = lookup.building(ctx, slots['building']) if slots.get('building') else None
        period = _label(slots['label'], start_day, end_day)
        where = f' · {building.name}' if building else ''
        note = _not_found(slots)
        if slots['measure'] == 'collection':
            if not ctx.allowed(MODULE_BILLING):
                return [say(_('You do not have access to payments.'))]
            payments = ctx.user.scope_queryset(
                Payment.objects.filter(tenant=ctx.tenant), building_field='tenancy__building',
                floor_field='tenancy__floor', apartment_field='tenancy__apartment')
            if building is not None:
                payments = payments.filter(tenancy__building=building)
            numbers = billing.collection_summary(ctx.tenant, start_day, end_day, payments=payments)
            text = note + _('Collected %(period)s%(where)s: %(total)s from %(count)s receipts.') % {
                'period': period, 'where': where, 'total': ctx.money(numbers['total']),
                'count': numbers['count']}
            rows = [(mode, ctx.money(amount)) for mode, amount in numbers['by_mode'].items()]
            if numbers['pending']:
                text += ' ' + _('%(count)s of them wait for approval.') % {
                    'count': numbers['pending']}
            return [say(text, rows=rows, buttons=[
                link(_('Open daily collection'), reverse('billing:daily_collection'))])]

        if not ctx.allowed(MODULE_ACCOUNTING):
            return [say(_('You do not have access to income and expense.'))]
        entries = ctx.user.scope_queryset(LedgerEntry.objects.filter(
            tenant=ctx.tenant, entry_date__range=(start_day, end_day)))
        if building is not None:
            entries = entries.filter(building=building)
        sums = totals(entries)
        rows = []
        if slots['measure'] == 'expense':
            text = note + _('Expenses %(period)s%(where)s: %(amount)s.') % {
                'period': period, 'where': where, 'amount': ctx.money(sums['expense'])}
            for row in (entries.filter(kind=KIND_EXPENSE).values('category__name')
                        .annotate(total=Sum('amount')).order_by('-total')[:8]):
                rows.append((row['category__name'], ctx.money(row['total'])))
        elif slots['measure'] == 'income':
            text = note + _('Income %(period)s%(where)s: %(amount)s.') % {
                'period': period, 'where': where, 'amount': ctx.money(sums['income'])}
        else:
            text = note + _('Profit %(period)s%(where)s: %(amount)s.') % {
                'period': period, 'where': where, 'amount': ctx.money(sums['net'])}
            rows = [(_('Income'), ctx.money(sums['income'])),
                    (_('Expenses'), ctx.money(sums['expense']))]
        return [say(text, rows=rows, buttons=[
            link(_('Open income & expense'), reverse('accounting:ledger'))])]


def _label(label, start_day, end_day):
    named = {'today': _('today'), 'yesterday': _('yesterday'), 'this week': _('this week'),
             'last week': _('last week'), 'this month': _('this month'),
             'last month': _('last month'), 'this year': _('this year'),
             'last year': _('last year')}
    if label in named:
        return named[label]
    if label:
        return _('in %(month)s') % {'month': label}
    return _('from %(start)s to %(end)s') % {'start': day(start_day), 'end': day(end_day)}
