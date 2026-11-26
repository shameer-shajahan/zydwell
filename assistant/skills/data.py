"""Any question about the data: "how many guests from India", "list guests in Ahli house",
"total deposit held", "payments by card this month", "who stays in 103".

A question is read into three parts — *what* it is about (guests, beds, rooms,
flats, buildings, invoices, payments, deposits, rent), *how* to answer (how
many, how much in total, or a list) and *which* ones (a building, a room or
flat, a nationality, a period, staying or left, paid or not, cash or card,
a guest). The answer comes from the same data the screens show this user.
"""
import re
from decimal import Decimal

from django.db.models import Count, F, Q, Sum
from django.urls import reverse
from django.utils.translation import gettext as _

from billing import services as billing
from billing.models import Invoice, Payment
from core.constants import MODULE_BILLING, MODULE_GUESTS, MODULE_PROPERTIES
from guests.models import DepositTransaction, Tenancy

from .. import lookup
from ..brain import entities, parse
from .base import Handoff, Skill, day, link, say
from .common import read, stay_place

LIMIT = 15

#: What a question is about, with the words that name it (checked in this order).
SUBJECTS = [
    ('deposits', r'\b(?:deposits?|security)\b'),
    ('payments', r'\b(?:payments?|receipts?|collections?|collected|received|paid\s+by)\b'),
    ('invoices', r'\b(?:invoices?|bills?)\b'),
    ('expenses', r'\b(?:expenses?|spent|spending|income|profit)\b'),
    ('documents', r'\b(?:documents?|passports?|visas?|emirates\s+id)\b'),
    ('beds', r'\bbeds?\b'),
    ('rooms', r'\brooms?\b'),
    ('flats', r'\b(?:apartments?|flats?|units?)\b'),
    ('buildings', r'\bbuildings?\b'),
    ('rent', r'\b(?:rent|rents)\b'),
    ('guests', r'\b(?:guests?|tenants?|people|persons?|members?|residents?|occupants?|workers?|'
               r'bachelors?|who|names?|staying|indians?|pakistanis?|filipinos?|nepalis?)\b'),
]
COUNT = re.compile(r'\b(?:how\s+many|number\s+of|no\.?\s+of|count|total\s+number)\b', re.I)
TOTAL = re.compile(r'\b(?:total|how\s+much|sum|amount|value|altogether|overall)\b', re.I)
LEFT = re.compile(r'\b(?:left|vacated|checked\s+out|check\s+out|moved\s+out|past|former|old)\b',
                  re.I)
NOTICE = re.compile(r'\bon\s+notice|\bnotice\b|\bleaving\b', re.I)
JOINED = re.compile(r'\b(?:joined|new|came|checked\s+in|check\s+ins?|arrived)\b', re.I)
UNPAID = re.compile(r'\b(?:unpaid|not\s+paid|pending|due|owing|outstanding|open|balance)\b', re.I)
OVERDUE = re.compile(r'\b(?:overdue|late)\b', re.I)
PAID = re.compile(r'\b(?:paid|settled|cleared)\b', re.I)
FREE = re.compile(r'\b(?:free|empty|vacant|available)\b', re.I)
TAKEN = re.compile(r'\b(?:occupied|taken|full|booked)\b', re.I)


def subject_of(text):
    low = parse.normalise(text or '').lower()
    for subject, pattern in SUBJECTS:
        if re.search(pattern, low):
            return subject
    return None


def looks_like_a_question(text):
    low = (text or '').lower()
    return bool(re.match(r'\s*(?:how|what|which|who|whose|where|when|list|show|give|tell|count|'
                         r'total|number|any|is|are|do|does|names?)\b', low)) or low.rstrip().endswith('?')


class DataQuestion(Skill):
    name = 'data_question'
    module = ''               # each subject checks its own module
    confirm = False
    title = _('Ask about the data')
    example = _('how many guests from India / total deposit held / who stays in 103')

    # ------------------------------------------------------------ reading
    def start(self, ctx, text):
        low = parse.normalise(text or '').lower()
        subject = subject_of(text)
        if subject == 'expenses':
            raise Handoff('money')
        if subject == 'documents':
            raise Handoff('documents')
        found = read(ctx, text, building=True, place='codes')
        slots = {'subject': subject or 'guests', 'text': text,
                 'how': 'count' if COUNT.search(low) else 'total' if TOTAL.search(low) else 'list'}
        if found.get('building') is not None:
            slots['building'] = found['building'].pk
        if found.get('codes'):
            places, _unused = lookup.match_places(ctx, found['codes'], found.get('building'))
            rooms = {obj.room_id if kind == 'bed' else obj.pk for kind, obj in places}
            if rooms:
                slots['rooms'] = sorted(rooms)[:50]
        nationality = entities._NATIONALITY_RE.search(low)
        if nationality:
            slots['nationality'] = entities.NATIONALITY[nationality.group(1).lower()]
        gender = entities._GENDER_RE.search(low)
        if gender:
            slots['gender'] = entities.GENDER[gender.group(1).lower()]
        start_day, end_day, label = parse.find_period(text, ctx.today)
        # A period means something for money moving and for who came or went —
        # not for "total monthly rent" or "how many rooms".
        timed = slots['subject'] in ('payments', 'invoices', 'deposits') or (
            slots['subject'] == 'guests' and (LEFT.search(low) or JOINED.search(low)))
        if start_day and timed:
            slots.update(start=start_day.isoformat(), end=end_day.isoformat(), label=label)
        mode = lookup.find_mode(ctx, low)
        if mode is not None and slots['subject'] in ('payments', 'deposits'):
            slots['mode'] = mode.pk
        for key, pattern in (('left', LEFT), ('notice', NOTICE), ('joined', JOINED),
                             ('unpaid', UNPAID), ('overdue', OVERDUE), ('paid', PAID),
                             ('free', FREE), ('taken', TAKEN)):
            if pattern.search(low):
                slots[key] = True
        guests = lookup.find_guests(ctx, found['rest']) if slots['subject'] in (
            'invoices', 'payments', 'deposits', 'rent') else []
        if len(guests) == 1:
            slots['guest'] = guests[0].pk
        return slots

    def run(self, ctx, slots):
        handler = getattr(self, f"_{slots['subject']}")
        return handler(ctx, slots)

    # ------------------------------------------------------------ helpers
    def _period(self, slots):
        from datetime import date
        if not slots.get('start'):
            return None, None, ''
        return (date.fromisoformat(slots['start']), date.fromisoformat(slots['end']),
                slots.get('label', ''))

    def _where(self, ctx, slots):
        """A few words for the filters, so the answer says what it counted."""
        bits = []
        if slots.get('nationality'):
            bits.append(slots['nationality'])
        if slots.get('building'):
            building = lookup.building(ctx, slots['building'])
            if building:
                bits.append(_('in %(place)s') % {'place': building.name})
        if slots.get('rooms'):
            rooms = list(lookup.rooms(ctx).filter(pk__in=slots['rooms'])[:3])
            bits.append(_('in %(rooms)s') % {'rooms': ', '.join(lookup.room_label(r) for r in rooms)})
        start, end, label = self._period(slots)
        if start:
            bits.append(label or f'{day(start)} – {day(end)}')
        return (' (' + ', '.join(bits) + ')') if bits else ''

    def _stays(self, ctx, slots):
        """Stays this user may see: staying now, or left / joined in the period."""
        start, end, _label = self._period(slots)
        if slots.get('left'):
            qs = ctx.user.scope_queryset(Tenancy.objects.filter(
                tenant=ctx.tenant, status__code='vacated'))
            if start:
                qs = qs.filter(actual_check_out__range=(start, end))
        elif slots.get('joined') and start:
            qs = ctx.user.scope_queryset(Tenancy.objects.filter(
                tenant=ctx.tenant, check_in_date__range=(start, end)))
        else:
            qs = lookup.live_stays(ctx)
            if slots.get('notice'):
                qs = qs.filter(status__code='notice')
        qs = qs.select_related('guest', 'building', 'floor', 'apartment', 'room', 'bed', 'status')
        if slots.get('building'):
            qs = qs.filter(building_id=slots['building'])
        if slots.get('rooms'):
            qs = qs.filter(room_id__in=slots['rooms'])
        if slots.get('nationality'):
            # Saved as typed: "India", "Indian", "india" are all one nationality.
            names = {slots['nationality'].lower()} | {
                alias for alias, label in entities.NATIONALITY.items()
                if label == slots['nationality']}
            match = Q()
            for name in names:
                match |= Q(guest__nationality__iexact=name)
            qs = qs.filter(match)
        if slots.get('gender'):
            qs = qs.filter(guest__gender=slots['gender'])
        if slots.get('guest'):
            qs = qs.filter(guest_id=slots['guest'])
        return qs

    def _no(self, module):
        return [say(_('You do not have access to that.'))]

    # ------------------------------------------------------------ subjects
    def _guests(self, ctx, slots):
        if not ctx.allowed(MODULE_GUESTS):
            return self._no(MODULE_GUESTS)
        stays = self._stays(ctx, slots)
        what = (_('guests who left') if slots.get('left') else
                _('guests on notice') if slots.get('notice') else
                _('guests who joined') if slots.get('joined') and slots.get('start') else
                _('guests staying'))
        count = stays.count()
        text = _('%(count)s %(what)s%(where)s.') % {'count': count, 'what': what,
                                                   'where': self._where(ctx, slots)}
        if slots['how'] == 'total':
            rent = stays.aggregate(s=Sum('monthly_rent'))['s'] or Decimal('0')
            text += ' ' + _('Their rent adds up to %(amount)s a month.') % {'amount': ctx.money(rent)}
        rows = [] if slots['how'] == 'count' and count > LIMIT else [
            (s.guest.full_name, f'{stay_place(s)} · {_("since")} {day(s.check_in_date)}')
            for s in stays.order_by('guest__full_name')[:LIMIT]]
        if count > LIMIT and rows:
            text += ' ' + _('Showing the first %(n)s.') % {'n': LIMIT}
        return [say(text, rows=rows, buttons=[link(_('Open guests'), reverse('guests:guest_list'))])]

    def _rent(self, ctx, slots):
        if not ctx.allowed(MODULE_GUESTS):
            return self._no(MODULE_GUESTS)
        if slots.get('unpaid') or slots.get('overdue'):
            raise Handoff('overdue')
        stays = self._stays(ctx, slots)
        rent = stays.aggregate(s=Sum('monthly_rent'))['s'] or Decimal('0')
        return [say(_('Monthly rent of the %(count)s guests staying%(where)s: %(amount)s.') % {
            'count': stays.count(), 'where': self._where(ctx, slots), 'amount': ctx.money(rent)})]

    def _beds(self, ctx, slots):
        if not ctx.allowed(MODULE_PROPERTIES):
            return self._no(MODULE_PROPERTIES)
        beds = lookup.beds(ctx)
        if slots.get('building'):
            beds = beds.filter(room__apartment__floor__building_id=slots['building'])
        if slots.get('rooms'):
            beds = beds.filter(room_id__in=slots['rooms'])
        what = _('beds')
        if slots.get('free'):
            beds = beds.filter(status__counts_as_available=True).exclude(
                status__counts_as_occupied=True)
            what = _('free beds')
        elif slots.get('taken'):
            beds = beds.filter(status__counts_as_occupied=True)
            what = _('occupied beds')
        count = beds.count()
        rows = [] if slots['how'] == 'count' and count > LIMIT else [
            (lookup.bed_label(b), b.status.name if b.status else '—')
            for b in beds.order_by('room__apartment__floor__building__name', 'room__number',
                                   'label')[:LIMIT]]
        return [say(_('%(count)s %(what)s%(where)s.') % {
            'count': count, 'what': what, 'where': self._where(ctx, slots)}, rows=rows,
            buttons=[link(_('Open bed board'), reverse('properties:bed_board'))])]

    def _rooms(self, ctx, slots):
        if not ctx.allowed(MODULE_PROPERTIES):
            return self._no(MODULE_PROPERTIES)
        if slots.get('rooms') and slots['how'] == 'list':
            return self._guests(ctx, slots)          # "who is in room 101"
        rooms = lookup.rooms(ctx)
        if slots.get('building'):
            rooms = rooms.filter(apartment__floor__building_id=slots['building'])
        rooms = rooms.annotate(beds_total=Count('beds', filter=Q(beds__is_archived=False)))
        count = rooms.count()
        rows = [] if slots['how'] == 'count' and count > LIMIT else [
            (lookup.room_label(r), _('%(n)s beds') % {'n': r.beds_total})
            for r in rooms.order_by('apartment__floor__building__name', 'apartment__name',
                                    'number')[:LIMIT]]
        return [say(_('%(count)s rooms%(where)s.') % {'count': count,
                                                      'where': self._where(ctx, slots)},
                    rows=rows)]

    def _flats(self, ctx, slots):
        if not ctx.allowed(MODULE_PROPERTIES):
            return self._no(MODULE_PROPERTIES)
        if slots.get('rooms') and slots['how'] == 'list':
            return self._guests(ctx, slots)          # "who is in flat 103"
        rooms = lookup.rooms(ctx)
        if slots.get('building'):
            rooms = rooms.filter(apartment__floor__building_id=slots['building'])
        flats = sorted({(r.apartment.floor.building.name, r.apartment.name) for r in rooms})
        rows = [] if slots['how'] == 'count' and len(flats) > LIMIT else [
            (name, building) for building, name in flats[:LIMIT]]
        return [say(_('%(count)s apartments%(where)s.') % {'count': len(flats),
                                                           'where': self._where(ctx, slots)},
                    rows=rows)]

    def _buildings(self, ctx, slots):
        if not ctx.allowed(MODULE_PROPERTIES):
            return self._no(MODULE_PROPERTIES)
        buildings = list(lookup.buildings(ctx))
        return [say(_('%(count)s buildings.') % {'count': len(buildings)},
                    rows=[(b.name, b.code or '') for b in buildings[:LIMIT]])]

    def _invoices(self, ctx, slots):
        if not ctx.allowed(MODULE_BILLING):
            return self._no(MODULE_BILLING)
        if (slots.get('unpaid') or slots.get('overdue')) and slots['how'] == 'list' \
                and not slots.get('guest'):
            raise Handoff('overdue')
        invoices = ctx.user.scope_queryset(
            Invoice.objects.filter(tenant=ctx.tenant), building_field='tenancy__building',
            floor_field='tenancy__floor', apartment_field='tenancy__apartment'
        ).select_related('tenancy__guest')
        if slots.get('building'):
            invoices = invoices.filter(tenancy__building_id=slots['building'])
        if slots.get('guest'):
            invoices = invoices.filter(tenancy__guest_id=slots['guest'])
        start, end, _label = self._period(slots)
        if start:
            invoices = invoices.filter(period_start__range=(start, end))
        what = _('invoices')
        if slots.get('overdue'):
            invoices, what = billing.owing(invoices).filter(due_date__lt=ctx.today), _('overdue invoices')
        elif slots.get('unpaid'):
            invoices, what = billing.owing(invoices), _('unpaid invoices')
        elif slots.get('paid'):
            invoices, what = invoices.filter(status__counts_as_settled=True), _('paid invoices')
        count = invoices.count()
        total = sum((i.balance if (slots.get('unpaid') or slots.get('overdue')) else i.total
                     for i in invoices), Decimal('0'))
        text = _('%(count)s %(what)s%(where)s, %(total)s.') % {
            'count': count, 'what': what, 'where': self._where(ctx, slots),
            'total': ctx.money(total)}
        rows = [] if slots['how'] == 'count' and count > LIMIT else [
            (f'{i.number} · {i.tenancy.guest.full_name}',
             f'{i.period_label()} · {ctx.money(i.balance if i.balance > 0 else i.total)}')
            for i in invoices.order_by('-period_start')[:LIMIT]]
        return [say(text, rows=rows, buttons=[link(_('Open invoices'),
                                                   reverse('billing:invoice_list'))])]

    def _payments(self, ctx, slots):
        if not ctx.allowed(MODULE_BILLING):
            return self._no(MODULE_BILLING)
        payments = ctx.user.scope_queryset(
            Payment.objects.filter(tenant=ctx.tenant, is_reversed=False),
            building_field='tenancy__building', floor_field='tenancy__floor',
            apartment_field='tenancy__apartment').select_related('tenancy__guest', 'mode')
        start, end, label = self._period(slots)
        if not start:                                 # no period said: today
            start, end, label = ctx.today, ctx.today, 'today'
            slots.update(start=start.isoformat(), end=end.isoformat(), label=label)
        payments = payments.filter(paid_on__range=(start, end))
        if slots.get('building'):
            payments = payments.filter(tenancy__building_id=slots['building'])
        if slots.get('guest'):
            payments = payments.filter(tenancy__guest_id=slots['guest'])
        if slots.get('mode'):
            payments = payments.filter(mode_id=slots['mode'])
        count = payments.count()
        total = payments.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        text = _('%(count)s payments%(where)s, %(total)s.') % {
            'count': count, 'where': self._where(ctx, slots), 'total': ctx.money(total)}
        rows = [] if slots['how'] == 'count' and count > LIMIT else [
            (f'{p.receipt_number} · {p.tenancy.guest.full_name}',
             f'{ctx.money(p.amount)} · {p.mode or ""} · {day(p.paid_on)}')
            for p in payments.order_by('-paid_on', '-pk')[:LIMIT]]
        return [say(text, rows=rows, buttons=[link(_('Open payments'),
                                                   reverse('billing:payment_list'))])]

    def _deposits(self, ctx, slots):
        if not ctx.allowed(MODULE_GUESTS):
            return self._no(MODULE_GUESTS)
        start, end, _label = self._period(slots)
        if start or slots.get('mode'):
            moves = DepositTransaction.objects.filter(
                tenancy__in=self._stays(ctx, dict(slots, start=None)) if not slots.get('left')
                else self._stays(ctx, slots), kind=DepositTransaction.KIND_HOLD)
            if start:
                moves = moves.filter(happened_on__range=(start, end))
            if slots.get('mode'):
                moves = moves.filter(mode_id=slots['mode'])
            total = moves.aggregate(s=Sum('amount'))['s'] or Decimal('0')
            rows = [(f'{m.receipt_number} · {m.tenancy.guest.full_name}',
                     f'{ctx.money(m.amount)} · {day(m.happened_on)}')
                    for m in moves.select_related('tenancy__guest').order_by('-happened_on')[:LIMIT]]
            return [say(_('%(count)s deposits taken%(where)s, %(total)s.') % {
                'count': moves.count(), 'where': self._where(ctx, slots),
                'total': ctx.money(total)}, rows=rows)]
        from guests.services import with_deposit_held
        stays = with_deposit_held(self._stays(ctx, slots)).annotate(
            short=F('deposit_amount') - F('deposit_held_total'))
        held = sum((s.deposit_held_total or Decimal('0') for s in stays), Decimal('0'))
        agreed = sum((s.deposit_amount for s in stays), Decimal('0'))
        rows = [(s.guest.full_name, _('held %(held)s of %(agreed)s') % {
            'held': ctx.money(s.deposit_held_total or 0), 'agreed': ctx.money(s.deposit_amount)})
            for s in stays.order_by('-short', 'guest__full_name')[:LIMIT]
            if slots['how'] != 'count']
        return [say(_('Deposits%(where)s: %(held)s held of %(agreed)s agreed, %(short)s still to '
                      'collect.') % {'where': self._where(ctx, slots), 'held': ctx.money(held),
                                     'agreed': ctx.money(agreed),
                                     'short': ctx.money(max(agreed - held, Decimal('0')))},
                    rows=rows)]
