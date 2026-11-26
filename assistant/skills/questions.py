"""Questions: "where is Ravi", "free beds in Sunrise", "who has not paid", "today's collection".

Each answers at once, from the same data the matching screen shows the user
— their buildings, their modules — and offers the obvious next step.
"""
import calendar
import re
from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse
from django.utils.translation import gettext as _

from accounting.models import KIND_EXPENSE, LedgerEntry, totals
from billing import services as billing
from billing.models import Invoice, Payment
from core.constants import MODULE_ACCOUNTING, MODULE_BILLING, MODULE_GUESTS, MODULE_PROPERTIES
from guests.models import GuestDocument, Tenancy
from properties.models import occupancy_for, occupancy_grouped

from .. import lookup
from ..brain import parse
from .base import Finish, Handoff, Skill, day, link, say, start
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


def _stay_in(ctx, kind, obj):
    """The live stay in a bed (or a room let whole) this user may see."""
    stays = lookup.live_stays(ctx)
    return (stays.filter(bed=obj) if kind == 'bed'
            else stays.filter(room=obj, bed__isnull=True)).first()


class GuestInfo(Skill):
    """A guest, or whoever is in a bed: where, since when, the rent and what is owed."""

    name = 'guest_info'
    module = MODULE_GUESTS
    confirm = False
    title = _('Guest details')
    example = _('how much rent is pending on Ravi / who is in 101 A A2')

    def start(self, ctx, text):
        slots = {}
        found = read(ctx, text, building=True, place='codes')
        take_guests(ctx, slots, found)
        if slots.get('guest') or slots.get('guest_options') or not found.get('codes'):
            return slots
        # No name, but a bed: "117 A A5", "how much pending on 103 a4".
        places, _unused = lookup.match_places(ctx, found['codes'], found.get('building'))
        stays = [(kind, obj, _stay_in(ctx, kind, obj)) for kind, obj in places[:8]]
        taken = [(kind, obj, stay) for kind, obj, stay in stays if stay]
        if len(taken) == 1:
            slots['guest'] = taken[0][2].guest_id
            slots.pop('who', None)
        elif taken:
            slots['guest_options'] = [stay.guest_id for _k, _o, stay in taken]
        elif len(places) == 1:
            kind, obj = places[0]
            slots['free_place'] = f'{kind[0]}{obj.pk}'
        return slots

    def preset(self, ctx, values):
        slots = {}
        if str(values.get('guest', '')).isdigit():
            pick_guest(ctx, slots, values['guest'])
        return slots

    def ask(self, ctx, slots):
        token = slots.get('free_place')
        if token:
            kind = 'bed' if token[0] == 'b' else 'room'
            obj = (lookup.bed if kind == 'bed' else lookup.room)(ctx, int(token[1:]))
            if obj is not None:
                buttons = []
                if ctx.allowed(MODULE_GUESTS, 'create'):
                    buttons.append(start(_('Check someone in'), 'check_in',
                                         **{kind: obj.pk}))
                text = _('%(place)s is free: nobody is staying there.') % {
                    'place': lookup.place_label(kind, obj)}
                status = getattr(obj, 'status', None)
                if kind == 'bed' and status is not None and not status.counts_as_available:
                    text = _('Nobody is staying in %(place)s, but the bed is marked "%(status)s".') % {
                        'place': lookup.place_label(kind, obj), 'status': status.name}
                    buttons = []
                raise Finish([say(text, buttons=buttons)])
        return ask_guest(ctx, slots, _('Which guest? Type their name, mobile number or bed '
                                       '(like "101 A A2").'))

    def answer(self, ctx, slots, key, text):
        found = read(ctx, text, building=True, place='codes')
        if take_guests(ctx, slots, found):
            return None
        fresh = self.start(ctx, text)
        if fresh.get('guest') or fresh.get('guest_options') or fresh.get('free_place'):
            slots.update(fresh)
            return None
        return answer_guest(ctx, slots, text)

    def pick(self, ctx, slots, key, value):
        pick_guest(ctx, slots, value)

    def run(self, ctx, slots):
        guest = lookup.guest(ctx, slots['guest'])
        stay = lookup.stay_of(ctx, guest)
        rows = [(_('Mobile'), guest.mobile)]
        if stay:
            rows += [(_('Staying at'), stay_place(stay)),
                     (_('Since'), day(stay.check_in_date)),
                     (_('Monthly rent'), ctx.money(stay.monthly_rent))]
            if stay.billing_cycle == stay.CYCLE_MONTHLY:
                rows.append((_('Bill day'), _('day %(day)s of each month') % {
                    'day': stay.billing_day}))
            if stay.status and stay.status.code == 'notice':
                rows.append((_('Leaving on'), day(stay.expected_check_out) or _('not set')))
        else:
            rows.append((_('Staying at'), _('Not staying now') if not ctx.user.is_staff_member
                         else _('Not staying in your buildings')))
        can_bill = ctx.allowed(MODULE_BILLING)
        owed = guest.outstanding_balance() if can_bill else None
        unbilled = billing.unbilled_rent(stay, ctx.today) if (can_bill and stay) else []
        text = guest.full_name
        if can_bill:
            not_invoiced = sum((row['rent'] for row in unbilled), Decimal('0'))
            owed += not_invoiced
            text = (_('%(name)s owes %(amount)s.') if owed > 0 else
                    _('%(name)s owes nothing.')) % {'name': guest.full_name,
                                                   'amount': ctx.money(owed)}
            if not_invoiced:
                text += ' ' + _('%(amount)s of it is rent not invoiced yet.') % {
                    'amount': ctx.money(not_invoiced)}
            rows.append((_('Owes'), ctx.money(owed)))
            rows += self._unpaid_rows(ctx, guest)
            rows += [(f"{day(row['period_start'])} – {day(row['period_end'])} · "
                      + _('not invoiced'), f"{ctx.money(row['rent'])} · {self._when(ctx, row['due_date'])}")
                     for row in unbilled[:4]]
            credit = guest.advance_credit()
            if credit > 0:
                rows.append((_('Advance credit'), ctx.money(credit)))
            if stay and stay.is_live:
                upcoming = self._next_rent(ctx, stay)
                if upcoming:
                    rows.append((_('Next rent'), upcoming))
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
        return [say(text, rows=rows, buttons=buttons)]

    def _unpaid_rows(self, ctx, guest):
        """Each unpaid invoice: how much is left on it, and when it is or was due."""
        invoices = billing.owing(Invoice.objects.filter(tenancy__guest=guest)).order_by(
            'due_date', 'pk')
        rows = []
        for invoice in invoices[:4]:
            rows.append((f'{invoice.number} · {invoice.period_label()}',
                         f'{ctx.money(invoice.balance)} · {self._when(ctx, invoice.due_date)}'))
        return rows

    @staticmethod
    def _when(ctx, due):
        late = (ctx.today - due).days
        return (_('%(days)s days late') % {'days': late} if late > 0 else
                _('due today') if late == 0 else _('due %(date)s') % {'date': day(due)})

    def _next_rent(self, ctx, stay):
        """The next rent not billed yet that falls due after today."""
        ahead = [row for row in billing.unbilled_rent(stay, ctx.today + timedelta(days=70))
                 if row['due_date'] > ctx.today]
        if not ahead:
            return ''
        return _('%(amount)s on %(date)s') % {'amount': ctx.money(ahead[0]['rent']),
                                              'date': day(ahead[0]['due_date'])}


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
        # "rent pending on akash" or "pending on 117 a5" asks about one guest.
        found = read(ctx, text, building=True, place='codes')
        if lookup.find_guests(ctx, found['rest'], found.get('mobile', '')) or (
                found.get('codes') and lookup.match_places(ctx, found['codes'],
                                                           found.get('building'))[0]):
            raise Handoff('guest_info')
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
        # Rent due that nobody has invoiced yet counts the same.
        stays = lookup.live_stays(ctx)
        if building is not None:
            stays = stays.filter(building=building)
        for item in billing.unbilled_for(stays, ctx.today):
            if slots.get('this_month'):
                if not (ctx.today.replace(day=1) <= item['period_start'] <= ctx.today):
                    continue
            elif item['due_date'] >= ctx.today:
                continue
            row = people.setdefault(item['tenancy'].pk, {
                'tenancy': item['tenancy'], 'amount': Decimal('0'), 'late': 0})
            row['amount'] += item['rent']
            row['late'] = max(row['late'], (ctx.today - item['due_date']).days)
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
    named = {'today': _('today'), 'yesterday': _('yesterday'), 'tomorrow': _('tomorrow'),
             'next week': _('next week'), 'next month': _('next month'), 'this week': _('this week'),
             'last week': _('last week'), 'this month': _('this month'),
             'last month': _('last month'), 'this year': _('this year'),
             'last year': _('last year')}
    if label in named:
        return named[label]
    if label:
        return _('in %(month)s') % {'month': label}
    return _('from %(start)s to %(end)s') % {'start': day(start_day), 'end': day(end_day)}


class Documents(Skill):
    """Passports, visas and IDs running out: "whose visa is expiring", "passport expiry list"."""

    name = 'documents'
    module = MODULE_GUESTS
    confirm = False
    title = _('Documents expiring')
    example = _('whose visa is expiring this month')

    def start(self, ctx, text):
        _start, end, label = parse.find_period(text, ctx.today)
        slots = {'until': (end or ctx.today + timedelta(days=30)).isoformat(),
                 'label': label}
        low = parse.normalise(text).lower()
        for kind in ('passport', 'visa', 'emirates_id'):
            if kind.replace('_', ' ') in low or (kind == 'emirates_id' and re.search(
                    r'\b(?:eid|emirates\s+id|id\s+card)\b', low)):
                slots['kind'] = kind
        return slots

    def run(self, ctx, slots):
        until = date.fromisoformat(slots['until'])
        docs = GuestDocument.objects.filter(
            tenant=ctx.tenant, expiry_date__isnull=False, expiry_date__lte=until,
        ).select_related('guest').order_by('expiry_date')
        if slots.get('kind'):
            docs = docs.filter(doc_type=slots['kind'])
        if ctx.user.is_staff_member:
            docs = docs.filter(guest_id__in=lookup.live_stays(ctx).values('guest_id'))
        docs = list(docs[:LIST_LIMIT + 1])
        if not docs:
            return [say(_('No documents run out by %(date)s.') % {'date': day(until)})]
        rows = []
        for doc in docs[:LIST_LIMIT]:
            left = (doc.expiry_date - ctx.today).days
            when = (_('expired %(days)s days ago') % {'days': -left} if left < 0 else
                    _('expires today') if left == 0 else
                    _('%(date)s, in %(days)s days') % {'date': day(doc.expiry_date), 'days': left})
            rows.append((f'{doc.guest.full_name} · {doc.get_doc_type_display()}', when))
        text = _('Documents running out by %(date)s:') % {'date': day(until)}
        if len(docs) > LIST_LIMIT:
            text += ' ' + _('Showing the first %(n)s.') % {'n': LIST_LIMIT}
        return [say(text, rows=rows, buttons=[
            link(_('Open expiring documents'), reverse('guests:expiring_documents'))])]


class Movements(Skill):
    """Who is leaving or who joined: "who is leaving this week", "new check-ins this month"."""

    module = MODULE_GUESTS
    confirm = False

    def __init__(self, name):
        self.name = name
        if name == 'leaving_list':
            self.title = _('Who is leaving')
            self.example = _('who is leaving this week')
        else:
            self.title = _('Who joined')
            self.example = _('who checked in this month')

    def start(self, ctx, text):
        start_day, end_day, label = parse.find_period(text, ctx.today)
        if self.name == 'leaving_list' and label in ('this week', 'this month', 'this year'):
            # Leaving "this week" is from today to its end, not the days gone by.
            start_day = ctx.today
            if label == 'this week':
                end_day = ctx.today + timedelta(days=6 - ctx.today.weekday())
            elif label == 'this month':
                end_day = ctx.today.replace(
                    day=calendar.monthrange(ctx.today.year, ctx.today.month)[1])
            else:
                end_day = ctx.today.replace(month=12, day=31)
        if start_day is None:
            if self.name == 'leaving_list':        # the coming week
                start_day, end_day, label = ctx.today, ctx.today + timedelta(days=7), ''
            else:                                  # this month so far
                start_day, end_day, label = ctx.today.replace(day=1), ctx.today, 'this month'
        slots, _found = _place_slots(ctx, text)
        slots.update({'start': start_day.isoformat(), 'end': end_day.isoformat(), 'label': label})
        return slots

    def run(self, ctx, slots):
        start_day, end_day = date.fromisoformat(slots['start']), date.fromisoformat(slots['end'])
        building = lookup.building(ctx, slots['building']) if slots.get('building') else None
        if self.name == 'leaving_list':
            stays = lookup.live_stays(ctx).filter(expected_check_out__range=(start_day, end_day))
            order, field = 'expected_check_out', 'expected_check_out'
        else:
            stays = ctx.user.scope_queryset(Tenancy.objects.filter(
                tenant=ctx.tenant, check_in_date__range=(start_day, end_day))).select_related(
                'guest', 'building', 'floor', 'apartment', 'room', 'bed')
            order, field = 'check_in_date', 'check_in_date'
        if building is not None:
            stays = stays.filter(building=building)
        stays = list(stays.order_by(order)[:LIST_LIMIT + 1])
        period = _label(slots['label'], start_day, end_day)
        if not stays:
            return [say(_not_found(slots) + (_('Nobody is leaving %(period)s.') if
                                             self.name == 'leaving_list' else
                                             _('Nobody checked in %(period)s.')) % {'period': period})]
        rows = [(f'{s.guest.full_name} · {stay_place(s)}', day(getattr(s, field)))
                for s in stays[:LIST_LIMIT]]
        text = _not_found(slots) + (_('Leaving %(period)s:') if self.name == 'leaving_list'
                                    else _('Checked in %(period)s:')) % {'period': period}
        buttons = []
        if self.name == 'leaving_list' and ctx.allowed(MODULE_GUESTS, 'edit'):
            buttons = [start(_('Check out %(name)s') % {'name': s.guest.full_name}, 'check_out',
                             tenancy=s.pk) for s in stays[:3] if s.expected_check_out <= ctx.today]
        return [say(text, rows=rows, buttons=buttons)]


class Approvals(Skill):
    """Money a collector took in, waiting for the owner: "pending approvals"."""

    name = 'approvals'
    module = ''               # the owner's page: checked in run()
    confirm = False
    title = _('Approvals waiting')
    example = _('payments waiting for approval')

    def run(self, ctx, slots):
        if not (ctx.user.is_admin or ctx.user.is_super_admin):
            return [say(_('Only the owner approves payments and deposits.'))]
        from guests.models import DepositTransaction

        payments = Payment.objects.filter(tenant=ctx.tenant, approval=Payment.APPROVAL_PENDING)
        deposits = DepositTransaction.objects.filter(tenant=ctx.tenant, approval='pending')
        p_total = payments.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        d_total = deposits.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        if not payments.exists() and not deposits.exists():
            return [say(_('Nothing is waiting for your approval.'))]
        rows = [(_('Payments'), _('%(count)s · %(amount)s') % {
                    'count': payments.count(), 'amount': ctx.money(p_total)}),
                (_('Deposits'), _('%(count)s · %(amount)s') % {
                    'count': deposits.count(), 'amount': ctx.money(d_total)})]
        for payment in payments.select_related('tenancy__guest', 'collected_by')[:5]:
            rows.append((f'{payment.receipt_number} · {payment.tenancy.guest.full_name}',
                         f'{ctx.money(payment.amount)} · {payment.collected_by or ""}'))
        return [say(_('Waiting for your approval:'), rows=rows,
                    buttons=[link(_('Open approvals'), reverse('billing:approvals'))])]
