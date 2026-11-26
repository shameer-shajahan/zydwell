"""Turn words into records: guests, live stays, beds, buildings, categories.

Everything is limited to the signed-in user's account, and stays, beds and
buildings to the places a staff member looks after, exactly as the screens
limit them. Names are matched forgivingly: "ravi", "Ravi K" and "ravee" all
find "Ravi Kumar".
"""
import difflib
import re
from datetime import timedelta

from accounting.models import KIND_EXPENSE, Category
from billing.models import PaymentMode
from guests.models import Guest, Tenancy
from properties.models import Room, visible_beds, visible_buildings

from .brain import classifier, entities

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
            # A letter missing or swapped ("aksh" for "akash") still counts.
            ratio = difflib.SequenceMatcher(None, word, token).ratio()
            if ratio >= CLOSE:
                best = max(best, ratio)
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


def newest_guests(ctx, days=30):
    """Guests added lately who are not staying anywhere yet, newest first."""
    since = ctx.today - timedelta(days=days)
    staying = Tenancy.objects.filter(tenant=ctx.tenant, status__code__in=Tenancy.LIVE_STATUS_CODES)
    return list(Guest.objects.filter(tenant=ctx.tenant, is_company=False, created_at__date__gte=since)
                .exclude(pk__in=staying.values('guest_id')).order_by('-created_at')[:8])


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
    """``(building, span)`` when the message names one of the user's buildings.

    The whole name ("Ahli house A"), or its first word when that is
    distinctive ("ahli"). The span is where it was found, so the caller can
    take out just those words — not every "a" in the message.
    """
    text = text or ''
    best, best_span = None, None
    for building in buildings(ctx):
        for label in filter(None, [building.name, building.code]):
            words = re.findall(r'[a-z0-9]+', label.lower())
            if not words or len(''.join(words)) < 2:
                continue
            match = re.search(r'\b' + r'\W+'.join(map(re.escape, words)) + r'\b', text, re.I)
            if match is None and len(words[0]) >= 4:
                match = re.search(r'\b' + re.escape(words[0]) + r'\b', text, re.I)
            if match and (best_span is None
                          or match.end() - match.start() > best_span[1] - best_span[0]):
                best, best_span = building, match.span()
    return best, best_span


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


def match_places(ctx, codes, building=None):
    """Beds — and rooms let whole — that fit the codes typed, best first.

    ``codes`` is ``[(code, kind), ...]`` from ``parse.place_codes``: "117"
    said to be an apartment, "a5" a bed, "a" a room, or with no kind at all.
    Each code is checked against the apartment, room and bed of every place
    this user can see. People mix the words up ("room 101" for flat 101), so
    a code that fits another field still counts, just less. The places that
    fit the most codes, best scored, come back; with them, the codes that
    fit none of those places (a mistyped flat, or a rent amount).
    Returns ``(places, unused_codes)``; a place is ``('bed', bed)`` or
    ``('room', room)``.
    """
    if not codes:
        return [], []
    qs = beds(ctx)
    if building is not None:
        qs = qs.filter(room__apartment__floor__building=building)
    candidates = [('bed', b, b.room.apartment.name, b.room.number, b.label) for b in qs]
    whole = rooms(ctx).filter(rent_basis__in=[Room.RENT_PER_ROOM, Room.RENT_PER_FLAT])
    if building is not None:
        whole = whole.filter(apartment__floor__building=building)
    candidates += [('room', r, r.apartment.name, r.number, '') for r in whole]

    ranked = []
    for kind, obj, apartment, room_no, label in candidates:
        fields = {'apartment': apartment.lower(), 'room': room_no.lower(), 'bed': label.lower()}
        joined = {room_no.lower() + label.lower(), f'{room_no}-{label}'.lower()} if label else set()
        matched, score, used = 0, 0.0, set()
        for code, said in codes:
            best = 0.0
            for field, value in fields.items():
                if value and code == value:
                    best = max(best, 3.0 if said == field else 2.0 if said is None else 1.0)
            if code in joined:
                best = max(best, 3.0)
            if best:
                used.add(code)
                score += best
        matched = len(used)
        if matched:
            ranked.append((matched, score, kind, obj, used))
    if not ranked:
        return [], [code for code, _said in codes]
    top = max((m, s) for m, s, *_rest in ranked)
    best = [(kind, obj, used) for m, s, kind, obj, used in ranked if (m, s) == top]
    used_any = set().union(*(used for _k, _o, used in best))
    unused = []
    for code, _said in codes:
        if code not in used_any and code not in unused:
            unused.append(code)
    return [(kind, obj) for kind, obj, _used in best], unused


def place_label(kind, obj):
    if kind == 'bed':
        return bed_label(obj)
    return room_label(obj)


def bed_label(bed_obj):
    room_obj = bed_obj.room
    apartment = room_obj.apartment
    parts = [apartment.floor.building.name]
    if apartment.name.lower() != room_obj.number.lower():
        parts.append(apartment.name)
    parts += [room_obj.number, bed_obj.label]
    return ' · '.join(parts)


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
    # income
    'utilities_share': ['laundry', 'washing machine', 'wifi share', 'internet share',
                        'utilities share', 'electricity share', 'utility share'],
    'key_replacement': ['key', 'keys', 'key card', 'card replacement', 'lost key', 'access card'],
    'late_payment_fee': ['late fee', 'fine', 'penalty'],
}


def find_category(ctx, text, kind=KIND_EXPENSE):
    """The category a message names: its own words ("plumber" is maintenance),
    or a near miss of one ("plumbr", "electrisity")."""
    typed = re.findall(r'[a-z&-]+', (text or '').lower())
    low = ' ' + ' '.join(typed) + ' '
    # Rent and deposits come in through payments, never by hand.
    cats = list(Category.objects.filter(tenant=ctx.tenant, kind=kind, is_active=True,
                                        is_system=False))
    best, best_len = None, 0
    single = {}
    for cat in cats:
        words = set(_CATEGORY_WORDS.get(cat.code, []))
        words.add(cat.name.lower())
        words.update(w for w in re.findall(r'[a-z]+', cat.name.lower()) if len(w) >= 5)
        for word in words:
            if f' {word} ' in low and len(word) > best_len:
                best, best_len = cat, len(word)
            if ' ' not in word and len(word) >= 5:
                single.setdefault(word, cat)
    if best is None:
        for word in typed:
            if len(word) >= 5:
                close = difflib.get_close_matches(word, list(single), n=1, cutoff=0.82)
                if close:
                    return single[close[0]]
    return best


def category(ctx, pk, kind=KIND_EXPENSE):
    return Category.objects.filter(tenant=ctx.tenant, kind=kind, is_active=True, pk=pk).first()


def fallback_category(ctx, kind=KIND_EXPENSE):
    """"Other expense" or "Other income", when nothing in the message names a category."""
    code = 'other_expense' if kind == KIND_EXPENSE else 'other_income'
    return Category.by_code(ctx.tenant, kind, code) or Category.objects.filter(
        tenant=ctx.tenant, kind=kind, is_active=True, is_system=False).first()


def mask_names(ctx, text):
    """A sentence without the guest and building names in it, to learn from.

    What is learnt is the way of saying something ("ravi gav notis" ->
    "gav notis"), not who it was about.
    """
    names = set()
    for full in Guest.objects.filter(tenant=ctx.tenant).values_list('full_name', flat=True):
        names.update(t.lower() for t in re.findall(r"[\w']+", full or '') if len(t) >= 2)
    _building, span = find_building(ctx, text)
    if span:
        text = text[:span[0]] + ' ' + text[span[1]:]
    known = classifier.vocabulary()
    kept = []
    for word in (text or '').split():
        bare = re.sub(r"[^\w']", '', word.lower())
        if bare and bare in names and bare not in known:
            continue
        kept.append(word)
    return ' '.join(kept)[:300]
