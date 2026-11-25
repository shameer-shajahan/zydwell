"""Pull guest details out of a typed sentence, with plain rules.

"add guest ravi kumar 050 123 4567 indian male works at emaar" gives
``{'full_name': 'Ravi Kumar', 'mobile': '0501234567', 'nationality': 'Indian',
'gender': 'male', 'employer': 'Emaar'}``.

Numbers, e-mails and the like have a shape a pattern can find. A name has no
shape, so it is what is left after everything else is taken out — every word
the model was trained on (``dataset.json``) and the filler words below are
known not to be names.
"""
import difflib
import re

from .classifier import vocabulary

# ---------------------------------------------------------------- patterns
EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
# A run of digits, maybe with + / 00 in front and spaces, dashes, dots or
# brackets between; 7 to 15 digits in all (the international limit).
PHONE = re.compile(r'(?<![\w+])(?:\+|00)?\d[\d\s().-]{5,20}\d(?!\w)')
EMPLOYER = re.compile(
    r"\b(?:works?|working|worker|job|employed|employee|staff)\s+(?:at|in|for|with|of)\s+"
    r"|\b(?:company|employer|office)\s*(?:is|name is|:|-)?\s+",
    re.I)
NAME_MARKER = re.compile(
    r"\b(?:(?:his|her|guest'?s?|the|full)\s+)?(?:name\s*(?:is|=|:|-|to)?|named|called|mr\.?|mrs\.?|ms\.?)\s+",
    re.I)

#: Words that end a name, an employer or any other free-text value.
FIELD_WORDS = {
    'mobile', 'mob', 'phone', 'ph', 'number', 'num', 'no', 'contact', 'whatsapp', 'tel', 'cell',
    'email', 'mail', 'nationality', 'nation', 'country', 'from', 'gender', 'sex', 'works', 'work',
    'working', 'company', 'employer', 'and', 'with', 'age', 'room', 'bed', 'flat', 'building',
}

#: Common words that are never a name, beyond those in the dataset.
FILLER = FIELD_WORDS | {
    'a', 'an', 'the', 'is', 'are', 'was', 'be', 'to', 'for', 'of', 'in', 'on', 'at', 'by', 'it',
    'he', 'she', 'him', 'his', 'her', 'hers', 'they', 'them', 'their', 'this', 'that', 'these',
    'i', 'me', 'my', 'we', 'us', 'our', 'you', 'your', 'u', 'pls', 'plz', 'please', 'kindly',
    'just', 'also', 'now', 'today', 'tomorrow', 'yesterday', 'tonight', 'here', 'there', 'name',
    'named', 'called', 'mr', 'mrs', 'ms', 'sir', 'madam', 'bro', 'details', 'detail', 'info',
    'data', 'profile', 'record', 'entry', 'came', 'come', 'comes', 'coming', 'joined', 'join',
    'joining', 'joins', 'arrived', 'arriving', 'staying', 'stay', 'will', 'has', 'have', 'had',
    'been', 'one', 'new', 'guest', 'guests', 'tenant', 'tenants', 'person', 'people', 'guy',
    'add', 'added', 'adding', 'register', 'create', 'make', 'put', 'save', 'enter', 'his',
    'id', 'emirates', 'passport', 'visa', 'dob', 'born', 'birthday', 'years', 'year', 'old',
    'change', 'update', 'set', 'correct', 'actually', 'sorry', 'wrong', 'should', 'not',
}

#: Command words that people misspell; a near miss of one is not a name.
COMMAND_WORDS = ['guest', 'tenant', 'register', 'please', 'person', 'joined', 'joining',
                 'staying', 'mobile', 'number', 'nationality', 'company']

# ---------------------------------------------------------------- lookups
_NATIONALITIES = {
    'Indian': ['india', 'indian'],
    'Pakistani': ['pakistan', 'pakistani', 'pak'],
    'Bangladeshi': ['bangladesh', 'bangladeshi', 'bangali', 'bengali'],
    'Nepali': ['nepal', 'nepali', 'nepalese'],
    'Sri Lankan': ['sri lanka', 'srilanka', 'sri lankan', 'srilankan', 'lankan'],
    'Filipino': ['philippines', 'philippine', 'filipino', 'filipina', 'pinoy', 'pinay'],
    'Egyptian': ['egypt', 'egyptian'],
    'Emirati': ['uae', 'emirati', 'emarati'],
    'Saudi': ['saudi', 'saudi arabia', 'ksa'],
    'Omani': ['oman', 'omani'],
    'Jordanian': ['jordan', 'jordanian'],
    'Syrian': ['syria', 'syrian'],
    'Lebanese': ['lebanon', 'lebanese'],
    'Sudanese': ['sudan', 'sudanese'],
    'Afghan': ['afghanistan', 'afghan', 'afghani'],
    'Indonesian': ['indonesia', 'indonesian'],
    'Kenyan': ['kenya', 'kenyan'],
    'Ugandan': ['uganda', 'ugandan'],
    'Nigerian': ['nigeria', 'nigerian'],
    'Ethiopian': ['ethiopia', 'ethiopian'],
    'Ghanaian': ['ghana', 'ghanaian'],
    'Chinese': ['china', 'chinese'],
    'British': ['uk', 'british', 'england', 'english'],
    'American': ['usa', 'america', 'american'],
    'Russian': ['russia', 'russian'],
    'Turkish': ['turkey', 'turkish'],
    'Iranian': ['iran', 'iranian'],
    'Iraqi': ['iraq', 'iraqi'],
    'Yemeni': ['yemen', 'yemeni'],
    'Moroccan': ['morocco', 'moroccan'],
    'Tunisian': ['tunisia', 'tunisian'],
    'Vietnamese': ['vietnam', 'vietnamese'],
    'Thai': ['thailand', 'thai'],
    'Myanmar': ['myanmar', 'burma', 'burmese'],
    'Cameroonian': ['cameroon', 'cameroonian'],
}
NATIONALITY = {alias: label for label, aliases in _NATIONALITIES.items() for alias in aliases}
# Longest first, so "sri lankan" wins over "lankan".
_NATIONALITY_RE = re.compile(
    r'\b(' + '|'.join(sorted(map(re.escape, NATIONALITY), key=len, reverse=True)) + r')\b', re.I)

GENDER = {
    'male': 'male', 'man': 'male', 'boy': 'male', 'gents': 'male', 'gent': 'male',
    'gentleman': 'male', 'mens': 'male',
    'female': 'female', 'woman': 'female', 'lady': 'female', 'ladies': 'female',
    'girl': 'female', 'women': 'female', 'womens': 'female',
}
_GENDER_RE = re.compile(r'\b(' + '|'.join(GENDER) + r')\b', re.I)

_TOKEN = re.compile(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'.-]*|\d+|[^\sA-Za-z\d]")


# ---------------------------------------------------------------- helpers
def _digits(value):
    return re.sub(r'\D', '', value)


def normalise_mobile(raw):
    """Keep a leading + and the digits; 00 in front means +."""
    raw = raw.strip()
    digits = _digits(raw)
    if raw.startswith('00'):
        return '+' + digits[2:]
    return ('+' if raw.startswith('+') else '') + digits


def same_mobile(a, b):
    """True when two numbers are the same line, written either way.

    0501234567, +971501234567 and 00971 50 123 4567 are one number: compare
    the last nine digits, which are what stays the same in the UAE and India.
    """
    a, b = _digits(a or ''), _digits(b or '')
    return bool(a) and bool(b) and a[-9:] == b[-9:]


def _phone_in(match):
    """The number in a run of digit groups, and where it ends in the match.

    "050 123 4567 1500" is a mobile and then a rent: groups are taken from
    the left only while the total stays a real length (10 digits written
    locally, 12 after a +, 14 after 00), so the rent is left alone.
    """
    raw = match.group(0)
    international = raw.startswith('+') or raw.startswith('00')
    limit = 14 if raw.startswith('00') else 12 if international else 10
    total, end = 0, 0
    for chunk in re.finditer(r'\S+', raw):
        count = len(_digits(chunk.group(0)))
        if total and total + count > limit:
            break
        total += count
        end = chunk.end()
        if total >= limit:
            break
    number = normalise_mobile(raw[:end])
    if 7 <= len(_digits(number)) <= 15:
        return number, end
    return None, 0


def _blank(text, span):
    """Replace a matched part with spaces so positions stay put."""
    start, end = span
    return text[:start] + ' ' * (end - start) + text[end:]


def _tidy_name(tokens):
    out = []
    for token in tokens:
        token = token.strip(".'-")
        if not token:
            continue
        # "ravi" and "RAVI" become "Ravi"; "McDonald" is left as typed.
        out.append(token.capitalize() if token.islower() or token.isupper() else token)
    return ' '.join(out)


def _is_filler(word):
    low = word.lower().strip(".'-")
    if not low or low in FILLER or low in vocabulary() or low in NATIONALITY or low in GENDER:
        return True
    if len(low) >= 4 and difflib.get_close_matches(low, COMMAND_WORDS, n=1, cutoff=0.8):
        return True
    return False


def _name_after_marker(text):
    """The words straight after "name is", "named", "called"... up to the next field."""
    match = NAME_MARKER.search(text)
    if not match:
        return ''
    taken = []
    for token in _TOKEN.findall(text[match.end():]):
        low = token.lower()
        if not token[0].isalpha() or low in FIELD_WORDS or low in NATIONALITY or low in GENDER:
            break
        taken.append(token)
        if len(taken) == 5:
            break
    return _tidy_name(taken)


def _name_from_leftovers(text):
    """The first run of words that are not commands, fields or filler."""
    run = []
    for token in _TOKEN.findall(text):
        if token[0].isalpha() and not _is_filler(token):
            run.append(token)
            if len(run) == 5:
                break
        elif run:
            break
    return _tidy_name(run)


# ---------------------------------------------------------------- the API
def extract(text, *, name='guess'):
    """Every detail found in ``text``.

    ``name`` says how hard to look for a name:

    * ``'guess'`` — after "name is ..." or, failing that, the leftover words.
      For the first message ("add guest ravi 0501234567").
    * ``'marker'`` — only after "name is", "named", "called". For a message
      sent while a summary waits for yes, where a stray word is not a name.
    * ``'whole'`` — the whole reply, less any number or e-mail. For the answer
      to "What is the guest's name?".
    """
    found = {}
    rest = text or ''

    for match in EMAIL.finditer(rest):
        found.setdefault('email', match.group(0).lower())
        rest = _blank(rest, match.span())

    for match in PHONE.finditer(rest):
        number, end = _phone_in(match)
        if number and 'mobile' not in found:
            found['mobile'] = number
            rest = _blank(rest, (match.start(), match.start() + end))

    employer = EMPLOYER.search(rest)
    if employer:
        taken, end = [], employer.end()
        for token in re.finditer(_TOKEN, rest[employer.end():]):
            word = token.group(0)
            low = word.lower()
            if not word[0].isalnum() or low in FIELD_WORDS or low in NATIONALITY or low in GENDER:
                break
            taken.append(word)
            end = employer.end() + token.end()
            if len(taken) == 6:
                break
        if taken:
            found['employer'] = ' '.join(w if not w.islower() else w.capitalize() for w in taken)
            rest = _blank(rest, (employer.start(), end))

    nationality = _NATIONALITY_RE.search(rest)
    if nationality:
        found['nationality'] = NATIONALITY[nationality.group(1).lower()]
        rest = _blank(rest, nationality.span())

    gender = _GENDER_RE.search(rest)
    if gender:
        found['gender'] = GENDER[gender.group(1).lower()]
        rest = _blank(rest, gender.span())

    if name == 'whole':
        cleaned = NAME_MARKER.sub(' ', rest)
        tokens = [t for t in _TOKEN.findall(cleaned) if t[0].isalpha()]
        # "yes" or "add guest" in answer to "what is the name?" is not a name;
        # "Will Smith" is, even though "will" alone is a common word.
        full_name = '' if all(map(_is_filler, tokens)) else _tidy_name(tokens[:6])
    else:
        full_name = _name_after_marker(rest)
        if not full_name and name == 'guess':
            full_name = _name_from_leftovers(rest)
    if full_name:
        found['full_name'] = full_name[:150]
    return found
