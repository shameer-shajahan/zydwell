"""The platform owner resetting a building owner's password."""
from core.models import AuditLog
from core.tests import ZyDwellTestCase


class PlatformPasswordResetTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        from accounts.models import User

        self.platform = User.objects.create_user(
            username='platform', password='pw-platform-123',
            role=User.ROLE_SUPER_ADMIN)
        self.url = f'/platform/users/{self.owner.pk}/password/'
        self.client.login(username='platform', password='pw-platform-123')

    # --- who may ----------------------------------------------------------
    def test_only_the_platform_owner_may(self):
        self.client.logout()
        self.client.login(username='owner', password='pw-owner-123')
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_certainly_may_not(self):
        self.client.logout()
        self.grant('staff', can_view=True, can_edit=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get(self.url).status_code, 403)

    # --- setting one ------------------------------------------------------
    def test_no_old_password_is_asked_for(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('Current password', html)
        self.assertIn('New password', html)

    def test_a_chosen_password_works_immediately(self):
        self.client.post(self.url, {'password': 'Chosen-Pass-9912',
                                    'confirm': 'Chosen-Pass-9912'})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('Chosen-Pass-9912'))

    def test_a_blank_field_generates_one_and_shows_it_once(self):
        response = self.client.post(self.url, {'password': '', 'confirm': ''})
        issued = response.context['issued']
        self.assertTrue(issued)
        self.assertGreaterEqual(len(issued), 10)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(issued))
        self.assertContains(response, issued)

        # Reloading the page must not show it again.
        self.assertIsNone(self.client.get(self.url).context['issued'])

    def test_mismatched_repeats_are_refused(self):
        self.client.post(self.url, {'password': 'Chosen-Pass-9912',
                                    'confirm': 'something-else'})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('pw-owner-123'))

    def test_a_weak_password_is_refused(self):
        response = self.client.post(self.url, {'password': '1234', 'confirm': '1234'})
        self.assertTrue(response.context['form'].errors)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('pw-owner-123'))

    def test_only_building_owners_can_be_reset_this_way(self):
        self.assertEqual(
            self.client.get(f'/platform/users/{self.staff.pk}/password/').status_code, 404)

    # --- afterwards -------------------------------------------------------
    def test_the_owner_is_made_to_choose_their_own(self):
        self.client.post(self.url, {'password': 'Chosen-Pass-9912',
                                    'confirm': 'Chosen-Pass-9912'})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.must_change_password)

    def test_the_audit_log_records_it_without_the_password(self):
        self.client.post(self.url, {'password': 'Chosen-Pass-9912',
                                    'confirm': 'Chosen-Pass-9912'})
        entry = AuditLog.objects.filter(summary__contains='Password reset').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.platform)
        self.assertNotIn('Chosen-Pass-9912', entry.summary)
        self.assertNotIn('Chosen-Pass-9912', str(entry.changes))

    def test_their_existing_sessions_stop_working(self):
        signed_in = self.client_class()
        signed_in.login(username='owner', password='pw-owner-123')
        self.assertEqual(signed_in.get('/').status_code, 200)

        self.client.post(self.url, {'password': 'Chosen-Pass-9912',
                                    'confirm': 'Chosen-Pass-9912'})
        self.assertEqual(signed_in.get('/').status_code, 302)


class MustChangePasswordTests(ZyDwellTestCase):
    """The flag has to bite, or a handed-out password keeps working forever."""

    def setUp(self):
        super().setUp()
        self.owner.must_change_password = True
        self.owner.save()
        self.client.login(username='owner', password='pw-owner-123')

    def test_every_screen_sends_them_to_set_a_password(self):
        for url in ['/', '/guests/', '/billing/invoices/', '/settings/']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn('/accounts/profile/', response['Location'], url)

    def test_the_profile_page_itself_still_opens(self):
        self.assertEqual(self.client.get('/accounts/profile/').status_code, 200)

    def test_they_can_still_sign_out(self):
        self.assertEqual(self.client.post('/accounts/logout/').status_code, 302)

    def test_changing_it_lifts_the_block(self):
        self.client.post('/accounts/profile/', {
            'action': 'password', 'current_password': 'pw-owner-123',
            'new_password': 'Brand-New-Pass-42', 'confirm_password': 'Brand-New-Pass-42',
        })
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.must_change_password)
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_nobody_else_is_affected(self):
        self.client.logout()
        self.grant('guests', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get('/guests/').status_code, 200)


class AdminResettingStaffTests(ZyDwellTestCase):
    """The same thing one level down: an owner resetting a collector."""

    def setUp(self):
        super().setUp()
        self.client.login(username='owner', password='pw-owner-123')

    def test_an_owner_sets_a_staff_password_without_the_old_one(self):
        self.client.post(f'/accounts/staff/{self.staff.pk}/edit/', {
            'username': self.staff.username, 'first_name': 'Staff', 'last_name': '',
            'email': '', 'phone': '', 'job_title': '', 'language': 'en',
            'is_active': 'on', 'password': 'Fresh-Staff-Pass-7',
        })
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password('Fresh-Staff-Pass-7'))

    def test_leaving_it_blank_keeps_the_current_one(self):
        self.client.post(f'/accounts/staff/{self.staff.pk}/edit/', {
            'username': self.staff.username, 'first_name': 'Renamed', 'last_name': '',
            'email': '', 'phone': '', 'job_title': '', 'language': 'en',
            'is_active': 'on', 'password': '',
        })
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.first_name, 'Renamed')
        self.assertTrue(self.staff.check_password('pw-staff-123'))

    def test_an_owner_cannot_reach_another_client_staff(self):
        from accounts.models import User
        from core.models import Tenant
        from core.seed import seed_tenant

        other = Tenant.objects.create(name='Other', slug='other-acc')
        seed_tenant(other, with_subscription=False)
        stranger = User.objects.create_user(
            username='stranger', password='pw-stranger-123', role=User.ROLE_STAFF,
            tenant=other)
        self.assertEqual(
            self.client.get(f'/accounts/staff/{stranger.pk}/edit/').status_code, 404)
