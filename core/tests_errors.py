"""Error pages. The 500 one is the trap: Django renders it with no request."""
from django.template import loader
from django.test import TestCase, override_settings

from core.tests import ZyDwellTestCase

LIVE = override_settings(DEBUG=False, ALLOWED_HOSTS=['*', 'testserver'])


class FiveHundredTests(TestCase):
    """django.views.defaults.server_error calls template.render() with no
    arguments, so there is no request and no context processors. Anything in
    500.html that reaches for request, user or the branding context renders
    empty at best and raises at worst — while the site is already broken."""

    def test_it_renders_with_no_request_at_all(self):
        html = loader.get_template('500.html').render()
        self.assertIn('500', html)
        self.assertIn('Something broke at our end', html)

    def test_it_still_links_somewhere_useful(self):
        html = loader.get_template('500.html').render()
        self.assertIn('href="/"', html)

    def test_it_carries_its_own_styling(self):
        html = loader.get_template('500.html').render()
        self.assertIn('css/app.css', html)
        self.assertIn('errcard', html)

    def test_it_promises_nothing_it_cannot_know(self):
        """No 'you are signed in as…' — there is no user on a 500."""
        html = loader.get_template('500.html').render()
        for leak in ['{{', 'None', 'AnonymousUser']:
            self.assertNotIn(leak, html)


@LIVE
class NotFoundTests(ZyDwellTestCase):
    def test_an_unknown_url_returns_the_custom_404(self):
        response = self.client.get('/no-such-page/')
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn('We cannot find that page', html)
        self.assertIn('errcard', html)

    def test_a_missing_record_returns_404_not_a_crash(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/guests/999999/')
        self.assertEqual(response.status_code, 404)
        self.assertIn('We cannot find that page', response.content.decode())

    def test_it_shows_the_address_that_failed(self):
        html = self.client.get('/guests/typo-here/').content.decode()
        self.assertIn('/guests/typo-here/', html)

    def test_a_signed_in_user_gets_somewhere_to_go(self):
        """The links offered are the ones that person can actually open."""
        self.client.login(username='owner', password='pw-owner-123')
        html = self.client.get('/no-such-page/').content.decode()
        self.assertIn('Or jump to', html)
        self.assertIn('Bed board', html)
        self.assertIn('Dashboard', html)

    def test_the_links_are_narrowed_to_what_a_collector_may_open(self):
        self.grant('guests', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        html = self.client.get('/no-such-page/').content.decode()
        self.assertIn('/guests/', html)
        self.assertNotIn('Invoices', html)
        self.assertNotIn('Staff &amp; access', html)

    def test_a_signed_out_visitor_is_pointed_at_sign_in(self):
        html = self.client.get('/no-such-page/').content.decode()
        self.assertIn('/accounts/login/', html)
        self.assertNotIn('Bed board', html)

    def test_it_is_not_indexed(self):
        self.assertIn('noindex', self.client.get('/no-such-page/').content.decode())


@LIVE
class ForbiddenTests(ZyDwellTestCase):
    def test_staff_hitting_an_admin_screen_get_the_custom_403(self):
        self.client.login(username='staff', password='pw-staff-123')
        response = self.client.get('/settings/')
        self.assertEqual(response.status_code, 403)
        html = response.content.decode()
        self.assertIn('You do not have access', html)
        self.assertIn('errcard', html)

    def test_it_repeats_the_reason_the_view_gave(self):
        self.client.login(username='staff', password='pw-staff-123')
        html = self.client.get('/settings/').content.decode()
        self.assertIn('Only the building owner', html)

    def test_staff_are_told_who_can_grant_access(self):
        self.grant('guests', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        html = self.client.get('/guests/new/').content.decode()
        self.assertIn('building owner grants access per module', html)

    def test_a_locked_account_still_gets_its_own_page(self):
        """Subscription lock has a dedicated screen, not the generic 403."""
        from datetime import timedelta

        from core.models import Subscription
        from django.utils import timezone

        Subscription.objects.create(
            tenant=self.tenant, expires_on=timezone.localdate() - timedelta(days=30),
            grace_days=7, auto_lock=True,
        )
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 403)
        self.assertIn('Account locked', response.content.decode())


class TemplateShapeTests(TestCase):
    def test_every_error_page_exists_and_renders(self):
        for name in ['400.html', '403.html', '403_csrf.html', '404.html', '500.html']:
            html = loader.get_template(name).render()
            self.assertIn('errcard', html, name)
            self.assertIn('ZyDwell', html, name)

    def test_the_page_direction_survives_having_no_context(self):
        """LANGUAGE_CODE comes from a context processor, which never runs on a
        500 — so the direction has to come from the active translation instead,
        or an Arabic user gets Arabic text laid out left to right."""
        from django.utils import translation

        with translation.override('ar'):
            html = loader.get_template('500.html').render()
            self.assertIn('dir="rtl"', html)
            self.assertIn('lang="ar"', html)
        with translation.override('en'):
            html = loader.get_template('500.html').render()
            self.assertIn('dir="ltr"', html)

    def test_the_error_text_is_translated(self):
        from django.utils import translation

        with translation.override('ar'):
            self.assertIn('لم نتمكن من العثور على هذه الصفحة',
                          loader.get_template('404.html').render())

    def test_they_all_respect_the_saved_theme(self):
        for name in ['400.html', '403.html', '404.html', '500.html']:
            html = loader.get_template(name).render()
            self.assertIn('zydwell-theme', html, name)
            self.assertLess(html.index('zydwell-theme'), html.index('css/app.css'), name)
