"""Misspelt and shortened command words, written out.

"appaertment" -> "apartment", "gust" -> "guest", "chekin" -> "check in".
Only the exact forms listed, or a near miss of a long command word, change;
every other word — a guest's name above all — is left exactly as typed.
"""
import calendar
import difflib
import re
from functools import lru_cache

#: Words people shorten or misspell, written out. Only these exact forms, or a
#: near miss of a long command word, are changed; a name is left alone.
SPELLING = {
    'aded': 'added', 'addd': 'add', 'gust': 'guest', 'gest': 'guest', 'guset': 'guest',
    'geust': 'guest', 'gst': 'guest', 'gusts': 'guests', 'gests': 'guests',
    'romm': 'room', 'rom': 'room', 'rum': 'room', 'roon': 'room', 'rooom': 'room',
    'bedd': 'bed', 'bedspace': 'bed space', 'bedspce': 'bed space',
    'appt': 'apartment', 'apt': 'apartment', 'aprt': 'apartment', 'apartmnt': 'apartment',
    'appartment': 'apartment', 'apparment': 'apartment', 'appartmet': 'apartment', 'flt': 'flat',
    'rnt': 'rent', 'depo': 'deposit', 'deposite': 'deposit', 'dposit': 'deposit',
    'chk': 'check', 'chek': 'check', 'chck': 'check', 'cheak': 'check', 'chec': 'check',
    'checkin': 'check in', 'chekin': 'check in', 'chkin': 'check in', 'checkinn': 'check in',
    'checkout': 'check out', 'chekout': 'check out', 'chkout': 'check out',
    'tdy': 'today', 'tody': 'today', 'tmrw': 'tomorrow', 'tmr': 'tomorrow', 'yday': 'yesterday',
    'rcvd': 'received', 'recd': 'received', 'recived': 'received', 'recieved': 'received',
    'pymt': 'payment', 'pmt': 'payment', 'amt': 'amount',
    'recntly': 'recently', 'recenty': 'recently', 'resently': 'recently', 'rcntly': 'recently',
    'frm': 'from', 'pls': 'please', 'plz': 'please', 'plss': 'please',
    'cheq': 'cheque', 'cheqe': 'cheque', 'chqe': 'cheque', 'bnk': 'bank', 'trnsfr': 'transfer',
    'trf': 'transfer', 'elec': 'electricity', 'electric': 'electricity', 'sal': 'salary',
    'mnth': 'month', 'mth': 'month', 'wk': 'week', 'yr': 'year', 'bal': 'balance',
    'pndg': 'pending', 'pend': 'pending', 'pent': 'pending', 'pendng': 'pending',
    'pnding': 'pending', 'ovrdue': 'overdue', 'dues': 'dues',
}
#: Long command words a typo is matched to. Six letters or more on both sides,
#: so a short name ("Akash") is never taken for a word ("cash").
KEYWORDS = sorted({
    'apartment', 'building', 'deposit', 'refund', 'received', 'receive', 'collected', 'collect',
    'payment', 'electricity', 'salary', 'maintenance', 'cleaning', 'notice', 'leaving', 'vacated',
    'vacate', 'tomorrow', 'yesterday', 'cheque', 'transfer', 'recently', 'tenant', 'expense',
    'expenses', 'collection', 'occupancy', 'occupied', 'available', 'overdue', 'outstanding',
    'balance', 'register', 'registered', 'checking', 'staying',
} | {calendar.month_name[i].lower() for i in range(1, 13)}
  | {calendar.day_name[i].lower() for i in range(7)})


@lru_cache(maxsize=4096)
def normalise_word(word):
    low = word.lower()
    if low in SPELLING:
        return SPELLING[low]
    if len(low) >= 6 and low not in KEYWORDS:
        close = difflib.get_close_matches(low, KEYWORDS, n=1, cutoff=0.84)
        if close and len(close[0]) >= 6:
            return close[0]
    return word


def normalise(text):
    """The message with misspelt command words written out ("appaertment" ->
    "apartment", "gust" -> "guest"); every other word as typed."""
    return re.sub(r'[A-Za-z]+', lambda m: normalise_word(m.group(0)), text or '')
