"""The sign-in screen: it is the one page every user meets, often on a phone."""
from django.test import TestCase

from accounts.models import LoginHistory, User
from core.models import Tenant
from core.seed import seed_tenant


class LoginPageTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name='Test Accommodation', slug='test-acc')
        seed_tenant(self.tenant, with_subscription=False)
        self.user = User.objects.create_user(
            username='supervisor1', password='pw-super-123', role=User.ROLE_ADMIN,
            tenant=self.tenant, first_name='Sam',
        )

    def get_html(self):
        return self.client.get('/accounts/login/').content.decode()

    # --- structure -----------------------------------------------------
    def test_the_labels_are_tied_to_their_inputs(self):
        html = self.get_html()
        for name in ('username', 'password'):
            self.assertIn(f'for="id_{name}"', html)
            self.assertIn(f'id="id_{name}"', html)

    def test_password_managers_and_phone_keyboards_are_told_what_to_do(self):
        html = self.get_html()
        self.assertIn('autocomplete="username"', html)
        self.assertIn('autocomplete="current-password"', html)
        self.assertIn('autocapitalize="none"', html)   # iOS capitalises by default
        self.assertIn('enterkeyhint', html)

    def test_the_page_offers_language_and_appearance_before_signing_in(self):
        """Staff may need Arabic or dark mode before they have an account context."""
        html = self.get_html()
        self.assertIn('name="language"', html)
        self.assertIn('data-theme-toggle', html)

    def test_the_helpers_that_stop_a_failed_sign_in_are_present(self):
        html = self.get_html()
        self.assertIn('data-reveal=', html)        # show the password
        self.assertIn('data-caps-hint', html)      # Caps Lock warning
        self.assertIn('data-submit-once', html)    # no double submit

    def test_it_is_not_indexed(self):
        self.assertIn('noindex', self.get_html())

    def test_the_theme_is_set_before_the_stylesheet_loads(self):
        html = self.get_html()
        self.assertLess(html.index('zydwell-theme'), html.index('css/app.css'))

    # --- behaviour -----------------------------------------------------
    def test_a_good_password_signs_you_in(self):
        response = self.client.post('/accounts/login/',
                                    {'username': 'supervisor1', 'password': 'pw-super-123'},
                                    follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['user'].is_authenticated)

    def test_a_padded_username_still_works(self):
        """Phone keyboards add a trailing space after autocomplete."""
        response = self.client.post('/accounts/login/',
                                    {'username': ' supervisor1 ', 'password': 'pw-super-123'},
                                    follow=True)
        self.assertTrue(response.context['user'].is_authenticated)

    def test_a_bad_password_explains_itself_and_keeps_the_username(self):
        response = self.client.post('/accounts/login/',
                                    {'username': 'supervisor1', 'password': 'nope'})
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('role="alert"', html)
        self.assertIn('Check for capital letters', html)
        self.assertIn('value="supervisor1"', html)   # no retyping
        self.assertIn('aria-invalid="true"', html)

    def test_a_wrong_username_reads_the_same_as_a_wrong_password(self):
        """Different wording would let anyone probe for valid staff usernames."""
        unknown = self.client.post('/accounts/login/',
                                   {'username': 'ghost', 'password': 'nope'}).content.decode()
        wrong_pw = self.client.post('/accounts/login/',
                                    {'username': 'supervisor1', 'password': 'nope'}).content.decode()
        message = 'That username and password do not match'
        self.assertIn(message, unknown)
        self.assertIn(message, wrong_pw)

    def test_a_disabled_account_is_refused(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.post('/accounts/login/',
                                    {'username': 'supervisor1', 'password': 'pw-super-123'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['user'].is_authenticated)

    def test_both_outcomes_reach_the_login_history(self):
        self.client.post('/accounts/login/', {'username': 'supervisor1', 'password': 'nope'})
        self.client.post('/accounts/login/',
                         {'username': 'supervisor1', 'password': 'pw-super-123'})
        self.assertEqual(LoginHistory.objects.filter(successful=False).count(), 1)
        self.assertEqual(LoginHistory.objects.filter(successful=True).count(), 1)

    def test_signing_in_again_just_goes_to_the_dashboard(self):
        self.client.login(username='supervisor1', password='pw-super-123')
        response = self.client.get('/accounts/login/')
        self.assertEqual(response.status_code, 302)
