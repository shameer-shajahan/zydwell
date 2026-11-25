"""Turn words into records: guests, live stays, beds, buildings, categories.

Everything is limited to the signed-in user's account, and stays, beds and
buildings to the places a staff member looks after, exactly as the screens
limit them. Names are matched forgivingly: "ravi", "Ravi K" and "ravee" all
find "Ravi Kumar".
"""
import difflib
import re

from accounting.models import KIND_EXPENSE, Category
from billing.models import PaymentMode
from guests.models import Guest, Tenancy
from properties.models import Room, visible_beds, visible_buildings

from .brain import entities

#: How close a typed word must be to a word of a name.
CLOSE = 0.78


# ---------------------------------------------------------------- names
def name_words(text):
    """Words in a message that may be (part of) a person's name."""
    words = []
    for token in re.findall(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'.-]*", text or ''):
        token = token.strip(".'-")
        if len(token) >= 2 and not entities._is_filler(token):
            words.append(token.lower())
    return words


def _word_score(word, name_tokens):
    best = 0.0
    for token in name_tokens:
        if word == token:
            return 1.0
        if len(word) >= 3 and token.startswith(word):
            best = max(best, 0.92)
        elif len(word) >= 3:
            ratio = difflib.SequenceMatcher(None, word, token).ratio()
            if ratio >= CLOSE:
                best = max(best, ratio * 0.95)
    return best


def rank_by_name(words, rows):
    """``rows`` is ``[(key, name), ...]``; returns the best-matching keys.

    A name needs at least one typed word that matches it well; words that
    match nothing (a stray "ref" or "pls") only lower its score, the same for
    every name. The best score wins; ties all come back, so the caller can
    ask which one.
    """
    if not words:
        return []
    scored = []
    for key, name in rows:
        tokens = [t.lower() for t in re.findall(r"[\w']+", name or '')]
        if not tokens:
            continue
        scores = [_word_score(word, tokens) for word in words]
        if max(scores) < 0.85:
            continue
        scored.append((sum(scores) / len(scores), key))
    if not scored:
        return []
    top = max(score for score, _key in scored)
    return [key for score, key in scored if score >= top - 0.02]


# ---------------------------------------------------------------- people
def live_stays(ctx):
    """The live stays this user may see — the same set the payment screen offers."""
    qs = Tenancy.objects.filter(
        tenant=ctx.tenant, status__code__in=Tenancy.LIVE_STATUS_CODES,
    ).select_related('guest', 'building', 'floor', 'apartment', 'room', 'bed', 'status')
    return ctx.user.scope_queryset(qs)


def find_stays(ctx, text, mobile=''):
    """Live stays whose guest the message names (or whose mobile it gives)."""
    stays = list(live_stays(ctx))
    if mobile:
        hits = [s for s in stays if entities.same_mobile(s.guest.mobile, mobile)]
        if hits:
            return hits
    keys = rank_by_name(name_words(text), [(s.pk, s.guest.full_name) for s in stays])
    by_pk = {s.pk: s for s in stays}
    return [by_pk[k] for k in keys]


def find_guests(ctx, text, mobile='', exclude_companies=True):
    rows = Guest.objects.filter(tenant=ctx.tenant)
    if exclude_companies:
        rows = rows.filter(is_company=False)
    if mobile:
        tail = re.sub(r'\D', '', mobile)[-4:]
        hits = [g for g in rows.filter(mobile__contains=tail)
                if entities.same_mobile(g.mobile, mobile)]
        if hits:
            return hits
    words = name_words(text)
    if not words:
        return []
    pairs = list(rows.values_list('pk', 'full_name'))
    keys = rank_by_name(words, pairs)
    return list(Guest.objects.filter(pk__in=keys).order_by('full_name')) if keys else []


def guest(ctx, pk):
    return Guest.objects.filter(tenant=ctx.tenant, pk=pk).first()


def stay(ctx, pk):
    return live_stays(ctx).filter(pk=pk).first()


def stay_of(ctx, guest_obj):
    """This guest's live stay, if this user may see it."""
    return live_stays(ctx).filter(guest=guest_obj).first()


# ---------------------------------------------------------------- places
def buildings(ctx):
    return visible_buildings(ctx.user, ctx.tenant).order_by('name')


def find_building(ctx, text):
    """``(building, words used)`` when the message names one of the user's buildings."""
    low = ' '.join(re.findall(r'[a-z0-9]+', (text or '').lower()))
    best, best_len = None, 0
    for building in buildings(ctx):
        for label in filter(None, [building.name, building.code]):
            words = re.findall(r'[a-z0-9]+', label.lower())
            if not words:
                continue
            phrase = ' '.join(words)
            # "sunrise tower", or "sunrise" alone when it is distinctive enough.
            if re.search(r'\b' + re.escape(phrase) + r'\b', low) and len(phrase) > best_len:
                best, best_len = building, len(phrase)
            elif len(words[0]) >= 4 and re.search(r'\b' + re.escape(words[0]) + r'\b', low) \
                    and len(words[0]) > best_len:
                best, best_len = building, len(words[0])
    if best is None:
        return None, []
    return best, re.findall(r'[a-z0-9]+', best.name.lower()) + re.findall(
        r'[a-z0-9]+', (best.code or '').lower())


def beds(ctx):
    return visible_beds(ctx.user, ctx.tenant)


def free_beds(ctx, building=None, room=None):
    qs = beds(ctx).filter(status__counts_as_available=True).exclude(
        status__counts_as_occupied=True)
    if building is not None:
        qs = qs.filter(room__apartment__floor__building=building)
    if room is not None:
        qs = qs.filter(room=room)
    return qs.order_by('room__apartment__floor__building__name', 'room__number', 'label')


def rooms(ctx):
    qs = Room.objects.filter(
        tenant=ctx.tenant, is_archived=False, apartment__is_archived=False,
        apartment__floor__is_archived=False, apartment__floor__building__is_archived=False,
    ).select_related('apartment', 'apartment__floor', 'apartment__floor__building')
    if ctx.user.is_staff_member:
        qs = ctx.user.scope_queryset(
            qs, building_field='apartment__floor__building', floor_field='apartment__floor',
            apartment_field='apartment')
    return qs


def find_rooms(ctx, number, building=None, flat=''):
    qs = rooms(ctx).filter(number__iexact=number)
    if building is not None:
        qs = qs.filter(apartment__floor__building=building)
    if flat:
        qs = qs.filter(apartment__name__iexact=flat)
    return list(qs.order_by('apartment__floor__building__name', 'apartment__name'))


def find_beds(ctx, label, room=None, building=None):
    qs = beds(ctx).filter(label__iexact=label)
    if room is not None:
        qs = qs.filter(room=room)
    if building is not None:
        qs = qs.filter(room__apartment__floor__building=building)
    return list(qs.order_by('room__number'))


def bed(ctx, pk):
    return beds(ctx).filter(pk=pk).first()


def room(ctx, pk):
    return rooms(ctx).filter(pk=pk).first()


def building(ctx, pk):
    return buildings(ctx).filter(pk=pk).first()


def bed_label(bed_obj):
    room_obj = bed_obj.room
    return f'{room_obj.apartment.floor.building.name} · {room_obj.number} · {bed_obj.label}'


def room_label(room_obj):
    return f'{room_obj.apartment.floor.building.name} · {room_obj.apartment.name} · {room_obj.number}'


# ---------------------------------------------------------------- money
_MODE_WORDS = {
    'cash': ['cash', 'hand', 'in hand', 'kash'],
    'card': ['card', 'credit card', 'debit card', 'visa card', 'master card', 'mastercard', 'pos',
             'swipe', 'swiped'],
    'upi': ['upi', 'gpay', 'google pay', 'phonepe', 'paytm'],
    'bank': ['bank', 'bank transfer', 'transfer', 'transferred', 'neft', 'imps', 'wire',
             'deposit to bank', 'account transfer'],
    'cheque': ['cheque', 'check', 'chq', 'pdc'],
    'online': ['online', 'link', 'payment link', 'apple pay', 'samsung pay', 'paypal'],
}


def find_mode(ctx, text):
    """The payment mode the message names, among this account's active modes."""
    low = ' ' + ' '.join(re.findall(r'[a-z]+', (text or '').lower())) + ' '
    # "check in" / "check out" are not cheques.
    low = re.sub(r' check (?:in|out) ', ' ', low)
    modes = list(PaymentMode.objects.filter(tenant=ctx.tenant, is_active=True))
    best, best_len = None, 0
    for mode in modes:
        words = set(_MODE_WORDS.get(mode.code, [])) | {mode.name.lower(), mode.code.lower()}
        for word in words:
            if f' {word} ' in low and len(word) > best_len:
                best, best_len = mode, len(word)
    return best


def mode(ctx, pk):
    return PaymentMode.objects.filter(tenant=ctx.tenant, is_active=True, pk=pk).first()


def default_mode(ctx):
    return PaymentMode.by_code(ctx.tenant, 'cash') or PaymentMode.default_for(ctx.tenant)


_CATEGORY_WORDS = {
    'electricity_water': ['electricity', 'electric', 'water', 'dewa', 'sewa', 'fewa', 'addc',
                          'aadc', 'power', 'utility', 'utilities', 'current bill', 'gas', 'ac bill'],
    'building_rent': ['building rent', 'mortgage', 'lease', 'landlord', 'owner rent', 'ejari'],
    'maintenance': ['maintenance', 'repair', 'repairs', 'repaired', 'fix', 'fixed', 'plumber',
                    'plumbing', 'electrician', 'carpenter', 'painting', 'paint', 'ac service',
                    'ac repair', 'leak', 'broken', 'lock', 'pest', 'pest control'],
    'cleaning': ['cleaning', 'cleaner', 'clean', 'soap', 'detergent', 'consumables', 'supplies',
                 'tissue', 'garbage', 'trash', 'laundry', 'washing'],
    'staff_salary': ['salary', 'salaries', 'wage', 'wages', 'staff pay', 'payroll', 'watchman',
                     'security guard', 'helper'],
    'internet_licence': ['internet', 'wifi', 'wi-fi', 'du', 'etisalat', 'e&', 'licence', 'license',
                         'municipality', 'trade licence', 'fees', 'permit'],
}


def find_category(ctx, text, kind=KIND_EXPENSE):
    low = ' ' + ' '.join(re.findall(r'[a-z&-]+', (text or '').lower())) + ' '
    cats = list(Category.objects.filter(tenant=ctx.tenant, kind=kind, is_active=True))
    best, best_len = None, 0
    for cat in cats:
        words = set(_CATEGORY_WORDS.get(cat.code, []))
        words.add(cat.name.lower())
        words.update(w for w in re.findall(r'[a-z]+', cat.name.lower()) if len(w) >= 5)
        for word in words:
            if f' {word} ' in low and len(word) > best_len:
                best, best_len = cat, len(word)
    return best


def category(ctx, pk, kind=KIND_EXPENSE):
    return Category.objects.filter(tenant=ctx.tenant, kind=kind, is_active=True, pk=pk).first()


def other_expense(ctx):
    return Category.by_code(ctx.tenant, KIND_EXPENSE, 'other_expense') or Category.objects.filter(
        tenant=ctx.tenant, kind=KIND_EXPENSE, is_active=True).first()
