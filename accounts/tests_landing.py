"""Where someone lands, and what they are offered when a screen refuses them."""
from django.utils import timezone

from core.models import AuditLog, FeatureFlag
from core.navigation import landing_url
from core.tests import ZyDwellTestCase


class LandingTests(ZyDwellTestCase):
    """Everyone has a dashboard; it just shows each person different things."""

    def sign_in(self, username, password):
        return self.client.post('/accounts/login/',
                                {'username': username, 'password': password})

    def test_an_owner_lands_on_the_dashboard(self):
        self.assertEqual(self.sign_in('owner', 'pw-owner-123')['Location'], '/')

    def test_so_does_a_collector(self):
        """They used to be dropped on a screen that refused them."""
        self.grant('guests', can_view=True)
        self.assertEqual(self.sign_in('staff', 'pw-staff-123')['Location'], '/')

    def test_and_one_with_no_permissions_at_all(self):
        self.assertEqual(self.sign_in('staff', 'pw-staff-123')['Location'], '/')

    def test_the_dashboard_opens_for_a_collector_rather_than_refusing(self):
        self.grant('guests', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'reports/staff_dashboard.html')

    def test_an_owner_gets_the_business_one(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/')
        self.assertTemplateUsed(response, 'reports/dashboard.html')

    def test_a_collector_with_the_reports_grant_gets_the_business_one_too(self):
        self.grant('reports', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertTemplateUsed(self.client.get('/'), 'reports/dashboard.html')

    def test_the_dashboard_is_in_everyones_menu(self):
        self.grant('guests', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(landing_url(self.staff, self.tenant), '/')

    def test_a_platform_owner_with_no_client_open_lands_on_the_console(self):
        from accounts.models import User

        User.objects.create_user(username='platform', password='pw-platform-123',
                                 role=User.ROLE_SUPER_ADMIN)
        self.assertEqual(self.sign_in('platform', 'pw-platform-123')['Location'],
                         '/platform/')

    def test_other_screens_still_refuse_rather_than_redirect(self):
        """Only the dashboard adapts; elsewhere a refusal is the point."""
        self.grant('guests', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get('/billing/invoices/').status_code, 403)


class StaffDashboardTests(ZyDwellTestCase):
    """It shows what this person can act on, and nothing else."""

    def setUp(self):
        super().setUp()
        self.client.login(username='staff', password='pw-staff-123')

    def labels(self, response):
        return [str(tile['label']) for tile in response.context['tiles']]

    def test_with_nothing_granted_it_says_so_instead_of_erroring(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['tiles'], [])
        self.assertContains(response, 'no screens yet')

    def test_a_guests_grant_brings_the_guest_tile(self):
        self.grant('guests', can_view=True)
        labels = self.labels(self.client.get('/'))
        self.assertIn('Guests you look after', labels)
        self.assertNotIn('You collected today', labels)

    def test_a_billing_grant_brings_the_money_tiles(self):
        self.grant('billing', can_view=True)
        labels = self.labels(self.client.get('/'))
        self.assertIn('You collected today', labels)
        self.assertIn('Rent still owed in your area', labels)

    def test_it_counts_only_what_that_person_collected(self):
        from decimal import Decimal

        from billing import services as billing
        from guests import services as guest_services

        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'))
        billing.record_payment(tenant=self.tenant, user=self.owner, tenancy=tenancy,
                               amount=Decimal('500'))
        billing.record_payment(tenant=self.tenant, user=self.staff, tenancy=tenancy,
                               amount=Decimal('200'))

        self.grant('billing', can_view=True)
        tiles = {str(t['label']): t['value'] for t in self.client.get('/').context['tiles']}
        self.assertIn('200', tiles['You collected today'])
        self.assertNotIn('700', tiles['You collected today'])

    def test_it_stays_inside_their_assigned_area(self):
        from decimal import Decimal

        from accounts.models import StaffAssignment
        from guests import services as guest_services
        from properties.models import Apartment, Bed, Room

        far = Apartment.objects.create(tenant=self.tenant, floor=self.floor, name='999')
        far_room = Room.objects.create(tenant=self.tenant, apartment=far, number='901',
                                       capacity=2)
        far_bed = Bed.objects.create(tenant=self.tenant, room=far_room, label='A')
        guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=far_room,
            bed=far_bed, check_in_date=timezone.localdate(), monthly_rent=Decimal('900'))

        StaffAssignment.objects.create(user=self.staff, building=self.building,
                                       floor=self.floor, apartment=self.apartment)
        self.grant('guests', can_view=True)
        tiles = {str(t['label']): t['value'] for t in self.client.get('/').context['tiles']}
        self.assertEqual(tiles['Guests you look after'], 0)

    def test_the_money_cards_stay_hidden_without_the_billing_grant(self):
        """Empty is not the same as forbidden.

        Left visible, the overdue card would say "nothing owed in your area"
        to someone who is simply not allowed to look at the money.
        """
        self.grant('guests', can_view=True)
        response = self.client.get('/')
        self.assertFalse(response.context['can_see_money'])
        self.assertNotContains(response, 'Nothing owed in your area')
        self.assertNotContains(response, 'You have not taken any money yet')
        self.assertNotContains(response, 'Chase these next')

    def test_and_appear_once_it_is_granted(self):
        self.grant('billing', can_view=True)
        response = self.client.get('/')
        self.assertTrue(response.context['can_see_money'])
        self.assertContains(response, 'Chase these next')
        self.assertContains(response, 'Nothing owed in your area')

    def test_the_quick_actions_follow_the_grants(self):
        self.grant('guests', can_view=True)
        self.assertFalse(self.client.get('/').context['can_check_in'])
        self.grant('guests', can_view=True, can_create=True)
        self.assertTrue(self.client.get('/').context['can_check_in'])


class RefusalPageTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.grant('guests', can_view=True)
        self.grant('billing', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')

    def test_it_offers_the_screens_they_can_open(self):
        from django.test import override_settings

        with override_settings(DEBUG=False, ALLOWED_HOSTS=['*', 'testserver']):
            html = self.client.get('/settings/').content.decode()
        self.assertIn('What you can open', html)
        self.assertIn('/guests/', html)
        self.assertNotIn('Go to the dashboard', html)


class SelectEverythingTests(ZyDwellTestCase):
    """One checkbox ticks the whole grid; saving is still a deliberate press."""

    def setUp(self):
        super().setUp()
        self.client.login(username='owner', password='pw-owner-123')
        self.url = f'/accounts/staff/{self.staff.pk}/access/'

    def test_the_tick_all_checkboxes_are_on_the_page(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('data-check-all="#permission-grid"', html)
        self.assertIn('data-check-col="1"', html)
        self.assertIn('data-check-row', html)

    def test_saving_a_fully_ticked_grid_grants_everything(self):
        from accounts.forms import STAFF_MODULES

        payload = {'action': 'permissions'}
        for module, _label in STAFF_MODULES:
            for action in ['view', 'create', 'edit', 'delete']:
                payload[f'{module}__{action}'] = 'on'
        self.client.post(self.url, payload)

        self.staff.refresh_from_db()
        for module, _label in STAFF_MODULES:
            for action in ['view', 'create', 'edit', 'delete']:
                self.assertTrue(self.staff.can(module, action), f'{module}.{action}')

    def test_an_empty_grid_takes_it_all_away(self):
        from accounts.forms import STAFF_MODULES

        self.grant('guests', can_view=True, can_create=True)
        self.client.post(self.url, {'action': 'permissions'})
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.module_permissions.count(), 0)
        self.assertFalse(self.staff.can('guests', 'view'))

    def test_the_admin_only_modules_are_not_even_in_the_grid(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('name="staff__view"', html)
        self.assertNotIn('name="settings__view"', html)

    def test_only_an_owner_may_change_permissions(self):
        self.client.logout()
        self.grant('staff', can_view=True, can_edit=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get(self.url).status_code, 403)
