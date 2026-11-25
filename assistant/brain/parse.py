"""Read amounts, dates, rooms and beds, and time periods out of plain English.

Pure functions on text: nothing here touches the database. ``lookup.py``
turns what is found into real guests, beds and buildings.
"""
import calendar
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .entities import EMAIL, PHONE, _blank

MONTHS = {}
for _number in range(1, 13):
    MONTHS[calendar.month_name[_number].lower()] = _number
    MONTHS[calendar.month_abbr[_number].lower()] = _number
MONTHS['sept'] = 9
_MONTH = r'(?:' + '|'.join(sorted(MONTHS, key=len, reverse=True)) + r')'
WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
WEEKDAYS.update({name.lower(): i for i, name in enumerate(calendar.day_abbr)})
_WEEKDAY = r'(?:' + '|'.join(sorted(WEEKDAYS, key=len, reverse=True)) + r')'

_CURRENCY = r'(?:aed|dhs?|dirhams?|rs\.?|inr|usd|sar|qar|omr|kwd|bhd)'
_NUMBER = r'\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?'


# ---------------------------------------------------------------- dates
_DATE_PATTERNS = [
    # 30/11, 30-11-2026, 30.11.26
    (re.compile(r'\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b'), 'dmy'),
    # 30 nov, 30th november 2026
    (re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(' + _MONTH + r')\.?(?:,?\s+(\d{4}))?\b',
                re.I), 'd_mon'),
    # nov 30, november 30th 2026
    (re.compile(r'\b(' + _MONTH + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b', re.I),
     'mon_d'),
    # on 30th, the 30th, 30th
    (re.compile(r'\b(?:on\s+(?:the\s+)?|the\s+)?(\d{1,2})(?:st|nd|rd|th)\b', re.I), 'ordinal'),
    # on 30
    (re.compile(r'\bon\s+(?:the\s+)?(\d{1,2})\b(?!\s*(?:aed|dhs?|dirhams?|%))', re.I), 'ordinal'),
    # date 1, from 15, since 3, joined on 20, check in date 5: a day, never money
    (re.compile(r'\b(?:date|dated|day|from|since|starting(?:\s+(?:on|from))?|starts?\s+(?:on|from)|'
                r'join(?:ed|ing|s)?\s+(?:on|from)|check\s*in\s+(?:date|on|from))\s*(?:is|:|=|-)?\s*'
                r'(?:the\s+)?(\d{1,2})\b(?!\s*(?:aed|dhs?|dirhams?|%|[/.-]\d|\.\d))', re.I), 'ordinal'),
]
_RELATIVE = [
    (re.compile(r'\bday\s+after\s+tomorrow\b', re.I), 2),
    (re.compile(r'\bday\s+before\s+yesterday\b', re.I), -2),
    (re.compile(r'\b(?:today|tonight|now|this\s+morning|this\s+evening)\b', re.I), 0),
    (re.compile(r'\b(?:tomorrow|tmrw|tmr|tomorow|tommorow)\b', re.I), 1),
    (re.compile(r'\b(?:yesterday|yday|yesterdy)\b', re.I), -1),
]
_IN_DAYS = re.compile(r'\b(?:in|after)\s+(\d{1,3})\s+days?\b', re.I)
_WEEKDAY_RE = re.compile(r'\b(next|this|coming|last)?\s*(' + _WEEKDAY + r')\b', re.I)
_MONTH_END = re.compile(r'\b(?:end\s+of\s+(?:the\s+|this\s+)?month|month\s+end)\b', re.I)
_NEXT_MONTH_END = re.compile(r'\bend\s+of\s+next\s+month\b', re.I)


def _safe_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(text, today):
    if not text:
        return None
    year = int(text)
    return year + 2000 if year < 100 else year


def _nearest(today, month, day, year=None, forward=True):
    """A day and month with no year: the closest one, ahead when ``forward``."""
    if year:
        return _safe_date(year, month, day)
    if forward is None:
        options = [d for d in (_safe_date(today.year + k, month, day) for k in (-1, 0, 1)) if d]
        return min(options, key=lambda d: abs((d - today).days)) if options else None
    found = _safe_date(today.year, month, day)
    if found is None:
        return None
    if forward and found < today - timedelta(days=60):
        found = _safe_date(today.year + 1, month, day) or found
    elif not forward and found > today + timedelta(days=60):
        found = _safe_date(today.year - 1, month, day) or found
    return found


def find_date(text, today, *, forward=True):
    """``(date, span)`` for the first date in ``text``, or ``(None, None)``.

    ``forward`` breaks ties for a bare "30th" or "monday": ``True`` takes the
    coming one (a leaving date), ``False`` the last one (a payment date) and
    ``None`` the nearest (a check-in: "date 20" on 25 Nov is 20 Nov).
    """
    text = text or ''
    if _NEXT_MONTH_END.search(text):
        match = _NEXT_MONTH_END.search(text)
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return date(year, month, calendar.monthrange(year, month)[1]), match.span()
    match = _MONTH_END.search(text)
    if match:
        return date(today.year, today.month,
                    calendar.monthrange(today.year, today.month)[1]), match.span()
    for pattern, days in _RELATIVE:
        match = pattern.search(text)
        if match:
            return today + timedelta(days=days), match.span()
    match = _IN_DAYS.search(text)
    if match:
        return today + timedelta(days=int(match.group(1))), match.span()
    for pattern, shape in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            found = None
            if shape == 'dmy':
                day, month = int(match.group(1)), int(match.group(2))
                year = _year(match.group(3), today)
                if 1 <= month <= 12:
                    found = _nearest(today, month, day, year, forward)
            elif shape == 'd_mon':
                found = _nearest(today, MONTHS[match.group(2).lower().rstrip('.')],
                                 int(match.group(1)), _year(match.group(3), today), forward)
            elif shape == 'mon_d':
                found = _nearest(today, MONTHS[match.group(1).lower().rstrip('.')],
                                 int(match.group(2)), _year(match.group(3), today), forward)
            elif forward is None:
                day = int(match.group(1))
                found = day_of_month(day, today) if 1 <= day <= 31 else None
            else:
                day = int(match.group(1))
                if 1 <= day <= 31:
                    found = _safe_date(today.year, today.month, day)
                    if found and forward and found < today:
                        month = today.month % 12 + 1
                        found = _safe_date(today.year + (today.month == 12), month, day)
                    elif found and not forward and found > today:
                        month = (today.month - 2) % 12 + 1
                        found = _safe_date(today.year - (today.month == 1), month, day)
            if found:
                return found, match.span()
    match = _WEEKDAY_RE.search(text)
    if match:
        which = (match.group(1) or '').lower()
        target = WEEKDAYS[match.group(2).lower()]
        if which == 'last' or (not which and forward is False):
            back = (today.weekday() - target) % 7 or 7
            return today - timedelta(days=back), match.span()
        ahead = (target - today.weekday()) % 7 or 7
        if which == 'next' and ahead < 7 and (target - today.weekday()) % 7 == 0:
            ahead = 7
        return today + timedelta(days=ahead), match.span()
    return None, None


def day_of_month(day, today):
    """The date nearest today that falls on ``day``: "date 1" on 25 Nov is
    1 Dec, "date 20" is 20 Nov."""
    options = []
    for shift in (-1, 0, 1):
        month = (today.month - 1 + shift) % 12 + 1
        year = today.year + (today.month - 1 + shift) // 12
        found = _safe_date(year, month, day)
        if found:
            options.append(found)
    return min(options, key=lambda d: abs((d - today).days)) if options else None


# ---------------------------------------------------------------- periods
def find_period(text, today):
    """``(start, end, label)`` for "today", "this month", "last week", "in november"..."""
    low = (text or '').lower()

    def month_span(year, month):
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])

    if re.search(r'\byesterday\b', low):
        day = today - timedelta(days=1)
        return day, day, 'yesterday'
    if re.search(r'\btoday\b|\btoday\'?s\b|\btodays\b', low):
        return today, today, 'today'
    if re.search(r'\blast\s+week\b', low):
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6), 'last week'
    if re.search(r'\b(?:this\s+week|weekly)\b', low):
        start = today - timedelta(days=today.weekday())
        return start, today, 'this week'
    if re.search(r'\blast\s+month\b|\bprevious\s+month\b', low):
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        start, end = month_span(year, month)
        return start, end, 'last month'
    if re.search(r'\blast\s+year\b', low):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), 'last year'
    if re.search(r'\b(?:this\s+year|yearly|annual)\b', low):
        return date(today.year, 1, 1), today, 'this year'
    match = re.search(r'\b(' + _MONTH + r')\b(?:\s+(\d{4}))?', low)
    if match and not re.search(r'\d\s*(?:st|nd|rd|th)?\s+' + match.group(1), low):
        month = MONTHS[match.group(1)]
        year = int(match.group(2)) if match.group(2) else (
            today.year if month <= today.month else today.year - 1)
        start, end = month_span(year, month)
        return start, end, calendar.month_name[month] + f' {year}'
    if re.search(r'\b(?:this\s+month|monthly|month)\b', low):
        return date(today.year, today.month, 1), today, 'this month'
    return None, None, ''


# ---------------------------------------------------------------- spelling
from .spelling import normalise, normalise_word  # noqa: E402,F401  (kept here for callers)


# ---------------------------------------------------------------- places
ROOM = re.compile(
    r'\b(?:room|rm|r)\s*(?:no\.?|number|num|#)?\s*[:-]?\s*([a-z]?\d{1,5}[a-z]?)\b', re.I)
BED = re.compile(r'\b(?:bed|bd)\s*(?:no\.?|number|num|#)?\s*[:-]?\s*([a-z0-9]{1,4})\b'
                 r'|\bb\s*[:#-]?\s*(\d{1,2})\b', re.I)
#: Words that follow "bed" without being its label: "bed in room 101".
_NOT_A_LABEL = {'in', 'for', 'at', 'to', 'of', 'is', 'on', 'the', 'and', 'with', 'from', 'no',
                'rent', 'room', 'free', 'open', 'please', 'pls', 'now', 'today'}
FLAT = re.compile(r'\b(?:flat|apartment|apt|unit)\s*(?:no\.?|number|#)?\s*([a-z]?\d{1,5}[a-z]?)\b',
                  re.I)
#: "101-A", "101/a", "101a": a room and a bed in one.
ROOM_BED = re.compile(r'\b(\d{1,5})\s*[-/]\s*([a-z]|\d{1,2})\b|\b(\d{2,5})([a-jl-z])\b', re.I)


def find_place(text):
    """``({'room', 'bed', 'flat'}, rest)`` — numbers and labels as typed, and
    the text with them blanked out."""
    found, rest = {}, text or ''
    match = ROOM_BED.search(rest)
    if match:
        found['room'] = (match.group(1) or match.group(3)).upper()
        found['bed'] = (match.group(2) or match.group(4)).upper()
        rest = _blank(rest, match.span())
    for key, pattern in (('room', ROOM), ('bed', BED), ('flat', FLAT)):
        if key in found:
            continue
        for match in pattern.finditer(rest):
            value = next(g for g in match.groups() if g)
            if key == 'bed' and value.lower() in _NOT_A_LABEL:
                continue
            found[key] = value.upper()
            rest = _blank(rest, match.span())
            break
    return found, rest


_PLACE_WORDS = {'apartment': 'apartment', 'flat': 'apartment', 'unit': 'apartment',
                'room': 'room', 'rm': 'room', 'bed': 'bed', 'bedspace': 'bed', 'partition': 'bed'}
_SKIP = {'no', 'number', 'num', 'nr'}
_TOKEN = re.compile(r'[A-Za-z0-9]+')


def _is_code(token):
    """A place code: "117", "a5", "12b", "A" — a number, or letters and digits."""
    return bool(re.fullmatch(r'[a-z]{0,2}\d{1,5}[a-z]{0,2}\d{0,3}|[a-z]', token, re.I))


def place_codes(text):
    """Every apartment, room and bed code typed, in any order, with its kind.

    "apartment 117 a room a5 bed" gives ``[('117', 'apartment'), ('a', 'room'),
    ('a5', 'bed'), ('a5', 'room')]`` — a code is taken from either side of
    its word, and the database decides which reading fits. A lone letter
    counts only next to a place word, and not as the article in "a bed".
    Returns ``(codes, spans)``.
    """
    tokens = [(m.group(0), m.span()) for m in _TOKEN.finditer(text or '')]
    low = [t.lower() for t, _span in tokens]
    codes, spans = [], []

    def add(i, kind):
        word = low[i]
        if (word, kind) not in codes:
            codes.append((word, kind))
            spans.append(tokens[i][1])

    for i, word in enumerate(low):
        kind = _PLACE_WORDS.get(word)
        if kind is None:
            continue
        # After the word: "room A5", "room no 12".
        j = i + 1
        while j < len(low) and low[j] in _SKIP:
            j += 1
        if j < len(low) and _is_code(low[j]) and low[j] not in _PLACE_WORDS \
                and not (low[j] == 'a' and j + 1 < len(low) and low[j + 1] in _PLACE_WORDS):
            add(j, kind)
        # Before the word: "A5 bed", "117 apartment", "a room" after "117".
        k = i - 1
        if k >= 0 and _is_code(low[k]) and low[k] not in _PLACE_WORDS:
            single = len(low[k]) == 1
            article = single and low[k] == 'a' and not (k > 0 and _is_code(low[k - 1])
                                                         and any(c.isdigit() for c in low[k - 1]))
            if not article:
                add(k, kind)
    # A pair written together: "101-B", "101/b".
    for match in re.finditer(r'\b(\d{1,5})\s*[-/]\s*([a-z]{1,2}\d{0,3}|\d{1,2})\b', text or '', re.I):
        for group in (1, 2):
            code = match.group(group).lower()
            if (code, None) not in codes:
                codes.append((code, None))
                spans.append(match.span(group))
    # Codes with a digit and no place word nearby: "117 a5".
    taken = {span for span in spans}
    for i, word in enumerate(low):
        if tokens[i][1] in taken or word in _PLACE_WORDS:
            continue
        if any(c.isdigit() for c in word) and _is_code(word):
            codes.append((word, None))
            spans.append(tokens[i][1])
    return codes, spans


# ---------------------------------------------------------------- money
_TAGGED = {
    'deposit_received': re.compile(
        r'\b(?:deposit|security)\s+(?:paid|received|collected|given)\s*(?:of\s+)?' + _CURRENCY
        + r'?\s*(' + _NUMBER + r')|\b(?:paid|received|collected|took)\s+(?:a\s+)?(?:deposit|security)'
        r'\s*(?:of\s+)?' + _CURRENCY + r'?\s*(' + _NUMBER + r')|\b(?:deposit|security)\s*(?:of\s+)?'
        + _CURRENCY + r'?\s*(' + _NUMBER + r')\s*(?:paid|received|collected|given)\b', re.I),
    'deposit': re.compile(
        r'\b(?:deposit|security)\s*(?:amount|is|of|:|=)?\s*' + _CURRENCY + r'?\s*(' + _NUMBER
        + r')(k)?', re.I),
    'refund': re.compile(
        r'\b(?:refund|return|give\s+back|pay\s+back)\s*(?:of|is|:)?\s*' + _CURRENCY + r'?\s*('
        + _NUMBER + r')(k)?', re.I),
    'deduct': re.compile(
        r'\b(?:deduct|deduction|cut|keep|charge|minus|less)\s*(?:of|is|:)?\s*' + _CURRENCY
        + r'?\s*(' + _NUMBER + r')(k)?', re.I),
    'rent': re.compile(
        r'\b(?:rent|at|for|@|price|rate|monthly)\s*(?:is|of|:|=)?\s*' + _CURRENCY + r'?\s*('
        + _NUMBER + r')(k)?(?!\s*(?:st|nd|rd|th)\b)(?:\s*(?:aed|dhs?|dirhams?))?'
        r'(?:\s*(?:per|a|/)\s*month)?', re.I),
}
_PLAIN = re.compile(r'(?<![\w.,/-])' + _CURRENCY + r'?\s*(' + _NUMBER + r')(k)?\s*' + _CURRENCY
                    + r'?(?![\w/-])', re.I)


def to_decimal(raw, thousand=False):
    try:
        value = Decimal(raw.replace(',', ''))
    except (InvalidOperation, AttributeError):
        return None
    return value * 1000 if thousand else value


def find_money(text, tags=(), plain=True):
    """``({tag: Decimal}, [plain amounts], rest)``.

    ``tags`` picks which labelled amounts to look for ("rent 800",
    "deposit 500", "refund 300"); every other number that is not a phone,
    date, room or bed is a plain amount, in the order typed — unless
    ``plain`` is false, when the caller sorts the bare numbers out itself.
    """
    rest = text or ''
    for pattern in (EMAIL, PHONE):
        for match in pattern.finditer(rest):
            if pattern is PHONE and len(re.sub(r'\D', '', match.group(0))) < 7:
                continue
            rest = _blank(rest, match.span())
    tagged = {}
    for tag in tags:
        pattern = _TAGGED[tag]
        match = pattern.search(rest)
        if match:
            raw = next(g for g in match.groups() if g and g.lower() != 'k')
            value = to_decimal(raw, thousand=bool(match.lastindex and (match.group(
                match.lastindex) or '').lower() == 'k'))
            if value is not None:
                tagged[tag] = value
                rest = _blank(rest, match.span())
    if not plain:
        return tagged, [], rest
    plain = []
    for match in _PLAIN.finditer(rest):
        value = to_decimal(match.group(1), thousand=bool(match.group(2)))
        if value is not None and value > 0:
            plain.append(value)
            rest = _blank(rest, match.span())
    return tagged, plain, rest


# ---------------------------------------------------------------- references
# The keyword must be a whole word ("ref", not the start of "refund"), and a
# reference holds at least one digit.
REFERENCE = re.compile(
    r'\b(?:reference|ref|txn|transaction|trx|utr|cheque\s+(?:no|number)|chq(?:\s*no)?|auth|'
    r'receipt\s+(?:no|number))(?![a-z])\.?\s*(?:no\.?|number|id|is|:|#|-)?\s*'
    r'(?=[a-z0-9-]*\d)([a-z0-9][a-z0-9-]{2,30})\b', re.I)


def find_reference(text):
    match = REFERENCE.search(text or '')
    if not match:
        return '', text
    return match.group(1).upper(), _blank(text, match.span())


def text_without(text, spans):
    for span in spans:
        if span:
            text = _blank(text, span)
    return text
