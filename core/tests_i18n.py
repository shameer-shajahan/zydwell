"""The Arabic catalogue: guard against msgmerge's fuzzy guesses shipping."""
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.utils import translation

PO = Path(settings.BASE_DIR) / 'locale' / 'ar' / 'LC_MESSAGES' / 'django.po'


class ArabicCatalogueTests(TestCase):
    def setUp(self):
        self.po = PO.read_text(encoding='utf-8')

    def test_no_fuzzy_entries_survive(self):
        """makemessages copies a near-match into a new string and marks it fuzzy.

        Fuzzy entries are ignored at runtime, so the screen quietly falls back to
        English — but the wrong Arabic sits in the file waiting for someone to
        clear the flag. ("Show password" once carried "Change password".)
        """
        guessed = [
            re.search(r'^msgid "(.*)"', block, re.M).group(1)
            for block in self.po.split('\n\n')
            if re.search(r'^#,.*\bfuzzy\b', block, re.M)
            and re.search(r'^msgid "(.*)"', block, re.M)
        ]
        self.assertEqual(
            guessed, [],
            'a fuzzy entry is a guess, not a translation: correct it or empty the '
            f'msgstr for {guessed}',
        )

    def test_the_catalogue_compiles_and_is_wired_up(self):
        self.assertTrue((PO.parent / 'django.mo').exists(),
                        'run manage.py compilemessages -l ar')
        self.assertIn('ar', dict(settings.LANGUAGES))

    def test_arabic_plural_rules_are_declared(self):
        self.assertIn('nplurals=6', self.po)

    def test_the_words_on_the_sign_in_screen_are_translated(self):
        with translation.override('ar'):
            for english in ['Sign in', 'Username', 'Password', 'Show password',
                            'Caps Lock is on', 'Language']:
                self.assertNotEqual(
                    translation.gettext(english), english,
                    f'"{english}" is still English in Arabic',
                )

    #: English pairs that genuinely share one Arabic word. Anything not on this
    #: list is almost certainly msgmerge having copied a near-match into the
    #: wrong entry — that is how "Show password" once read "Change password".
    KNOWN_SYNONYMS = {
        ('Access', 'Permissions'),
        ('Active', 'Live'),
        ('Capacity', 'Cap'),
        ('Held now', 'Held right now'),
        ('Location', 'Where'),
        ('Mode', 'Paid by'),
        ('Net total', 'Net'),
        ('Mode', 'Payment mode'),
        ('Taken by', 'Received by'),
        ('Disabled', 'switched off'),
    }

    def test_a_translated_string_is_not_left_pointing_at_a_different_one(self):
        """Two different English strings sharing one Arabic translation.

        Case and plural variants of the same phrase are fine, and a short list
        of real synonyms is allowed by name. Everything else fails, so a bad
        merge is caught the first time it appears rather than once enough of
        them pile up.
        """
        def shape(text):
            return re.sub(r's$', '', text.strip().lower())

        pairs = re.findall(r'^msgid "([^"]+)"\nmsgstr "([^"]+)"$', self.po, re.M)
        seen = {}
        clashes = []
        for english, arabic in pairs:
            first = seen.setdefault(arabic, english)
            if first == english or shape(first) == shape(english):
                continue
            if (first, english) in self.KNOWN_SYNONYMS or \
                    (english, first) in self.KNOWN_SYNONYMS:
                continue
            clashes.append((first, english, arabic))

        self.assertEqual(
            clashes, [],
            'these two English strings share one Arabic translation, which usually '
            f'means a bad merge. Correct it, or add the pair to KNOWN_SYNONYMS: {clashes}',
        )

    def test_a_translation_keeps_every_placeholder_the_english_has(self):
        """A msgstr that drops %(n)s silently loses the number it was carrying.

        Django substitutes with %, and a string holding no specifier comes back
        from it unchanged rather than raising — so "%(n)s receipts" translated
        as plain "receipts" renders with the count simply gone. A merge once
        guessed exactly that for "%(n)s receipts" and "%(n)s invoices".
        """
        def placeholders(text):
            return set(re.findall(r'%\((\w+)\)[sd]', text))

        entries = re.findall(r'^msgid "(.*)"\nmsgstr "(.*)"$', self.po, re.M)
        lost = [
            (english, arabic, sorted(placeholders(english) - placeholders(arabic)))
            for english, arabic in entries
            if arabic and placeholders(english) - placeholders(arabic)
        ]
        self.assertEqual(
            lost, [],
            'the Arabic drops a placeholder the English fills in, so that value '
            f'vanishes from the screen: {lost}',
        )

    def test_a_translation_invents_no_placeholder_of_its_own(self):
        """The mirror case: %(x)s with nothing to fill it raises KeyError."""
        def placeholders(text):
            return set(re.findall(r'%\((\w+)\)[sd]', text))

        entries = re.findall(r'^msgid "(.*)"\nmsgstr "(.*)"$', self.po, re.M)
        invented = [
            (english, arabic, sorted(placeholders(arabic) - placeholders(english)))
            for english, arabic in entries
            if placeholders(arabic) - placeholders(english)
        ]
        self.assertEqual(
            invented, [],
            'the Arabic asks for a value the English never passes, which raises '
            f'KeyError when the page renders: {invented}',
        )
