"""The chrome: theming, the mobile shell, and the markup the phone layout needs."""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from core.tests import ZyDwellTestCase

CSS = Path(settings.BASE_DIR) / 'static' / 'css' / 'app.css'


def _block(css, selector):
    """The declarations inside the rule that starts with ``selector``."""
    start = css.index('{', css.index(selector)) + 1
    depth, cursor = 1, start
    while depth:
        if css[cursor] == '{':
            depth += 1
        elif css[cursor] == '}':
            depth -= 1
        cursor += 1
    return css[start:cursor - 1]


def _tokens(text):
    return set(re.findall(r'(--[\w-]+)\s*:', text))


class ThemeStylesheetTests(SimpleTestCase):
    """Guard rails so a later edit cannot half-break dark mode."""

    def setUp(self):
        self.css = CSS.read_text()

    def test_dark_is_declared_both_by_preference_and_by_choice(self):
        by_preference = _tokens(_block(self.css, ':root:not([data-theme="light"])'))
        by_choice = _tokens(_block(self.css, ':root[data-theme="dark"]'))
        self.assertTrue(by_preference)
        self.assertEqual(
            by_preference, by_choice,
            'the media-query palette and the [data-theme="dark"] palette must match, '
            'otherwise the toggle and the system preference disagree',
        )

    def test_no_colour_exists_only_in_dark_mode(self):
        light = _tokens(_block(self.css, ':root {'))
        dark = _tokens(_block(self.css, ':root[data-theme="dark"]'))
        self.assertEqual(
            dark - light, set(),
            'every token redefined for dark must have a light value on bare :root',
        )

    def test_the_light_palette_is_on_bare_root(self):
        light = _tokens(_block(self.css, ':root {'))
        for token in ['--bg', '--card', '--label', '--tint', '--separator', '--field']:
            self.assertIn(token, light)

    def test_both_schemes_announce_themselves_to_the_browser(self):
        self.assertIn('color-scheme: light', self.css)
        self.assertIn('color-scheme: dark', self.css)

    def test_the_phone_layout_exists(self):
        self.assertIn('.tabbar', self.css)
        self.assertIn('table.data.stacked', self.css)
        self.assertIn('env(safe-area-inset-bottom', self.css)
        self.assertIn('env(safe-area-inset-top', self.css)


class ChromeMarkupTests(ZyDwellTestCase):
    def test_the_theme_is_applied_before_first_paint(self):
        """Without this the page flashes light before the saved dark loads."""
        for url, needs_login in [('/accounts/login/', False), ('/', True)]:
            if needs_login:
                self.client.login(username='owner', password='pw-owner-123')
            html = self.client.get(url).content.decode()
            self.assertIn('zydwell-theme', html, url)
            self.assertLess(html.index('zydwell-theme'), html.index('css/app.css'),
                            f'{url}: the theme script must run before the stylesheet')

    def test_the_appearance_control_is_reachable(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.assertIn('data-theme-toggle', self.client.get('/').content.decode())
        profile = self.client.get('/accounts/profile/').content.decode()
        for mode in ['auto', 'light', 'dark']:
            self.assertIn(f'data-theme-set="{mode}"', profile)

    def test_the_mobile_shell_is_present(self):
        self.client.login(username='owner', password='pw-owner-123')
        html = self.client.get('/').content.decode()
        for needle in ['class="tabbar', 'class="scrim', 'data-burger', 'data-nav-close',
                       'viewport-fit=cover']:
            self.assertIn(needle, html)

    def test_every_list_table_can_be_stacked_on_a_phone(self):
        """The phone layout reads column headings, so list tables need a thead."""
        self.client.login(username='owner', password='pw-owner-123')
        for url in ['/guests/', '/billing/invoices/', '/billing/payments/', '/accounting/',
                    '/messaging/', '/reports/rent-roll/', '/settings/audit-log/',
                    '/accounts/staff/']:
            html = self.client.get(url).content.decode()
            body = html[html.index('<div class="content">'):]
            self.assertIn('<thead>', body, f'{url} has no column headings to stack by')


class PaginationTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username='owner', password='pw-owner-123')

    def test_two_lists_on_one_page_page_independently(self):
        from accounting.models import KIND_EXPENSE, KIND_INCOME, Category
        for n in range(30):
            Category.objects.create(tenant=self.tenant, kind=KIND_INCOME,
                                    name=f'Income {n:02}', code=f'inc-{n}')
        response = self.client.get('/accounting/categories/?income_page=2')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['income'].number, 2)
        self.assertEqual(response.context['expense'].number, 1)
        self.assertContains(response, 'income_page=1')

    def test_the_pager_keeps_the_search_and_encodes_it(self):
        from django.template import Context, Template
        from django.test import RequestFactory

        from core.utils import paginate
        request = RequestFactory().get('/x/', {'q': 'A & B', 'page': '2'})
        page = paginate(request, list(range(60)), 25)
        html = Template('{% include "partials/_pager.html" %}').render(
            Context({'page': page, 'request': request}))
        self.assertIn('q=A%20%26%20B&page=3', html)
        self.assertIn('q=A%20%26%20B&page=1', html)
