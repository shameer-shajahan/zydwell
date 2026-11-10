"""Shared fixtures plus tests for tenancy isolation, statuses and subscriptions."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import StaffAssignment, StaffPermission, User
from core.models import FeatureFlag, Sequence, Status, Subscription, Tenant
from core.seed import seed_tenant
from guests.models import Guest
from properties.models import Apartment, Bed, Building, Floor, Room, occupancy_for


class ZyDwellTestCase(TestCase):
    """Builds a small but complete client: 1 building, 1 floor, 2 rooms, 6 beds."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name='Test Accommodation', slug='test-acc')
        seed_tenant(self.tenant, with_subscription=False)
        self.owner = User.objects.create_user(
            username='owner', password='pw-owner-123', role=User.ROLE_ADMIN,
            tenant=self.tenant, first_name='Owner',
        )
        self.staff = User.objects.create_user(
            username='staff', password='pw-staff-123', role=User.ROLE_STAFF,
            tenant=self.tenant, first_name='Staff',
        )
        self.building = Building.objects.create(tenant=self.tenant, name='Test Tower')
        self.floor = Floor.objects.create(
            tenant=self.tenant, building=self.building, name='Floor 1')
        self.apartment = Apartment.objects.create(
            tenant=self.tenant, floor=self.floor, name='101')
        self.room = Room.objects.create(
            tenant=self.tenant, apartment=self.apartment, number='101', capacity=4,
            rent_basis=Room.RENT_PER_BED)
        self.flat = Room.objects.create(
            tenant=self.tenant, apartment=self.apartment, number='102', capacity=1,
            rent_basis=Room.RENT_PER_FLAT, monthly_rent=Decimal('5000'))
        self.beds = [
            Bed.objects.create(tenant=self.tenant, room=self.room, label=label,
                               monthly_price=Decimal('800'))
            for label in 'ABCD'
        ]
        self.guest = Guest.objects.create(
            tenant=self.tenant, full_name='Test Guest', mobile='0501234567')

    def grant(self, module, **actions):
        defaults = {'can_view': True}
        defaults.update(actions)
        return StaffPermission.objects.update_or_create(
            user=self.staff, module=module, defaults=defaults)[0]


class SeedTests(ZyDwellTestCase):
    def test_new_tenant_gets_its_master_data(self):
        self.assertEqual(Status.objects.filter(
            tenant=self.tenant, kind=Status.KIND_BED).count(), 5)
        self.assertEqual(Status.objects.filter(
            tenant=self.tenant, kind=Status.KIND_INVOICE).count(), 5)
        self.assertTrue(self.tenant.category_set.filter(code='monthly_rent').exists())
        self.assertTrue(self.tenant.roomtype_set.filter(code='bedspace').exists())
        self.assertEqual(
            FeatureFlag.objects.filter(tenant=self.tenant, enabled=True).count(),
            len(FeatureFlag.MODULES),
        )

    def test_seeding_twice_is_safe(self):
        seed_tenant(self.tenant, with_subscription=False)
        self.assertEqual(Status.objects.filter(
            tenant=self.tenant, kind=Status.KIND_BED, code='available').count(), 1)


class StatusBehaviourTests(ZyDwellTestCase):
    def test_occupancy_follows_the_behaviour_flags_not_the_name(self):
        available = Status.by_code(self.tenant, Status.KIND_BED, 'available')
        occupied = Status.by_code(self.tenant, Status.KIND_BED, 'occupied')
        maintenance = Status.by_code(self.tenant, Status.KIND_BED, 'maintenance')
        self.beds[0].status = occupied
        self.beds[0].save()
        self.beds[1].status = maintenance
        self.beds[1].save()
        for bed in self.beds[2:]:
            bed.status = available
            bed.save()

        stats = occupancy_for(Bed.objects.filter(room=self.room))
        self.assertEqual(stats['total'], 4)
        self.assertEqual(stats['occupied'], 1)
        self.assertEqual(stats['available'], 2)
        self.assertEqual(stats['other'], 1)
        self.assertEqual(stats['percent'], 25.0)

    def test_admin_can_add_a_status_without_code_changes(self):
        deep_clean = Status.objects.create(
            tenant=self.tenant, kind=Status.KIND_BED, code='deep_clean',
            name='Deep cleaning', color='#ff9500', counts_as_occupied=True,
        )
        self.beds[0].status = deep_clean
        self.beds[0].save()
        stats = occupancy_for(Bed.objects.filter(pk=self.beds[0].pk))
        self.assertEqual(stats['occupied'], 1)

    def test_a_new_bed_takes_the_default_status(self):
        bed = Bed.objects.create(tenant=self.tenant, room=self.flat, label='A')
        self.assertEqual(bed.status.code, 'available')


class SequenceTests(ZyDwellTestCase):
    def test_numbers_are_sequential_and_never_reused(self):
        seen = {Sequence.next_value(self.tenant, 'receipt', 'RCP') for _ in range(50)}
        self.assertEqual(len(seen), 50)

    def test_sequences_are_per_tenant(self):
        other = Tenant.objects.create(name='Other', slug='other')
        first = Sequence.next_value(self.tenant, 'receipt', 'RCP')
        second = Sequence.next_value(other, 'receipt', 'RCP')
        self.assertTrue(first.endswith('00001'))
        self.assertTrue(second.endswith('00001'))


class SubscriptionLockTests(ZyDwellTestCase):
    def test_account_is_blocked_once_the_grace_period_has_passed(self):
        today = timezone.localdate()
        Subscription.objects.create(
            tenant=self.tenant, expires_on=today - timedelta(days=30), grace_days=7,
            auto_lock=True,
        )
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'Account locked', status_code=403)

    def test_inside_the_grace_period_the_account_still_works(self):
        today = timezone.localdate()
        Subscription.objects.create(
            tenant=self.tenant, expires_on=today - timedelta(days=2), grace_days=7,
            auto_lock=True,
        )
        self.client.login(username='owner', password='pw-owner-123')
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_suspended_client_cannot_reach_the_app(self):
        self.tenant.status = Tenant.STATUS_SUSPENDED
        self.tenant.save()
        self.client.login(username='owner', password='pw-owner-123')
        self.assertEqual(self.client.get('/').status_code, 403)


class PermissionTests(ZyDwellTestCase):
    def test_staff_without_a_grant_is_refused(self):
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get('/guests/').status_code, 403)

    def test_a_view_grant_does_not_imply_create(self):
        self.grant('guests', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get('/guests/').status_code, 200)
        self.assertEqual(self.client.get('/guests/new/').status_code, 403)

    def test_staff_can_never_reach_admin_only_modules(self):
        self.grant('staff', can_view=True, can_edit=True)
        self.assertFalse(self.staff.can('staff', 'view'))
        self.assertFalse(self.staff.can('settings', 'view'))

    def test_a_disabled_module_blocks_even_the_owner(self):
        FeatureFlag.objects.filter(tenant=self.tenant, module='guests').update(enabled=False)
        self.client.login(username='owner', password='pw-owner-123')
        self.assertEqual(self.client.get('/guests/').status_code, 403)

    def test_floor_assignment_narrows_what_staff_sees(self):
        other_building = Building.objects.create(tenant=self.tenant, name='Second Tower')
        other_floor = Floor.objects.create(
            tenant=self.tenant, building=other_building, name='Floor 1')
        other_apartment = Apartment.objects.create(
            tenant=self.tenant, floor=other_floor, name='201')
        other_room = Room.objects.create(
            tenant=self.tenant, apartment=other_apartment, number='201', capacity=2)
        Bed.objects.create(tenant=self.tenant, room=other_room, label='A')

        StaffAssignment.objects.create(user=self.staff, building=self.building)
        from properties.models import visible_beds

        visible = visible_beds(self.staff, self.tenant)
        self.assertEqual(visible.count(), 4)
        self.assertNotIn(other_building.pk,
                         {b.room.apartment.floor.building_id for b in visible})

    def test_unassigned_staff_sees_every_building(self):
        from properties.models import visible_buildings

        self.assertEqual(visible_buildings(self.staff, self.tenant).count(), 1)
