"""Building → Floor → Apartment → Room → Bed."""
from decimal import Decimal

from django.db.utils import IntegrityError
from django.utils import timezone

from core.tests import ZyDwellTestCase
from guests import services as guest_services
from properties.models import Apartment, Bed, Floor, Room, occupancy_for


class HierarchyTests(ZyDwellTestCase):
    def test_a_room_knows_its_way_up_to_the_building(self):
        self.assertEqual(self.room.apartment, self.apartment)
        self.assertEqual(self.room.floor, self.floor)
        self.assertEqual(self.room.building, self.building)

    def test_a_bed_does_too(self):
        bed = self.beds[0]
        self.assertEqual(bed.room, self.room)
        self.assertEqual(bed.apartment, self.apartment)
        self.assertEqual(bed.floor, self.floor)
        self.assertEqual(bed.building, self.building)

    def test_the_full_label_names_every_level(self):
        label = self.beds[0].full_label()
        for part in [self.building.name, self.floor.name, self.apartment.name,
                     self.room.number, self.beds[0].label]:
            self.assertIn(part, label)

    def test_occupancy_rolls_up_through_the_apartment(self):
        self.assertEqual(self.apartment.occupancy()['total'], 4)
        self.assertEqual(self.floor.occupancy()['total'], 4)
        self.assertEqual(self.building.occupancy()['total'], 4)

    def test_two_apartments_on_one_floor_are_counted_together(self):
        second = Apartment.objects.create(
            tenant=self.tenant, floor=self.floor, name='102')
        room = Room.objects.create(
            tenant=self.tenant, apartment=second, number='201', capacity=2)
        Bed.objects.create(tenant=self.tenant, room=room, label='A')

        self.assertEqual(second.occupancy()['total'], 1)
        self.assertEqual(self.apartment.occupancy()['total'], 4)
        self.assertEqual(self.floor.occupancy()['total'], 5)

    def test_a_room_number_may_repeat_in_another_apartment(self):
        second = Apartment.objects.create(
            tenant=self.tenant, floor=self.floor, name='102')
        room = Room.objects.create(
            tenant=self.tenant, apartment=second, number='101', capacity=2)
        self.assertEqual(room.apartment, second)

    def test_but_not_twice_in_the_same_apartment(self):
        with self.assertRaises(IntegrityError):
            Room.objects.create(
                tenant=self.tenant, apartment=self.apartment, number='101', capacity=2)

    def test_an_apartment_name_is_unique_on_its_floor(self):
        with self.assertRaises(IntegrityError):
            Apartment.objects.create(
                tenant=self.tenant, floor=self.floor, name=self.apartment.name)


class TenancyPlacementTests(ZyDwellTestCase):
    def test_checking_in_records_the_whole_address(self):
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'))
        self.assertEqual(tenancy.apartment, self.apartment)
        self.assertEqual(tenancy.floor, self.floor)
        self.assertEqual(tenancy.building, self.building)

    def test_the_label_reads_building_floor_apartment_room_bed(self):
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'))
        self.assertEqual(
            tenancy.location_label(),
            f'{self.building.name} / {self.floor.name} / {self.apartment.name} / '
            f'{self.room.number} / {self.beds[0].label}')

    def test_a_transfer_updates_the_apartment_too(self):
        second = Apartment.objects.create(
            tenant=self.tenant, floor=self.floor, name='102')
        room = Room.objects.create(
            tenant=self.tenant, apartment=second, number='201', capacity=2)
        bed = Bed.objects.create(tenant=self.tenant, room=room, label='A',
                                 monthly_price=Decimal('800'))
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'))
        guest_services.transfer_bed(tenancy=tenancy, user=self.owner, new_bed=bed,
                                    new_room=room)
        tenancy.refresh_from_db()
        self.assertEqual(tenancy.apartment, second)


class ApartmentScreenTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username='owner', password='pw-owner-123')

    def test_the_floor_plan_groups_rooms_under_their_apartment(self):
        response = self.client.get(f'/properties/{self.building.pk}/')
        self.assertEqual(response.status_code, 200)
        block = response.context['floor_blocks'][0]
        self.assertEqual(block['floor'], self.floor)
        self.assertEqual(block['apartments'][0]['apartment'], self.apartment)
        self.assertEqual(len(block['apartments'][0]['rooms']), 2)
        self.assertContains(response, 'Apartment')

    def test_an_apartment_can_be_created_on_a_floor(self):
        self.client.post('/properties/apartments/new/', {
            'floor': self.floor.pk, 'name': '202', 'notes': '', 'sort_order': 2,
        })
        self.assertTrue(Apartment.objects.filter(floor=self.floor, name='202').exists())

    def test_a_room_is_created_inside_an_apartment(self):
        self.client.post('/properties/rooms/new/', {
            'apartment': self.apartment.pk, 'number': '999', 'capacity': 2,
            'rent_basis': 'per_bed', 'monthly_rent': '0', 'notes': '',
        })
        room = Room.objects.get(number='999')
        self.assertEqual(room.apartment, self.apartment)

    def test_an_apartment_in_use_is_archived_rather_than_deleted(self):
        guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'))
        self.client.post(f'/properties/apartments/{self.apartment.pk}/delete/')
        self.apartment.refresh_from_db()
        self.assertTrue(self.apartment.is_archived)
        self.assertTrue(Apartment.objects.filter(pk=self.apartment.pk).exists())

    def test_the_bed_board_shows_the_apartment_band(self):
        response = self.client.get('/properties/board/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.apartment.name)

    def test_the_occupancy_report_breaks_down_by_apartment(self):
        response = self.client.get('/reports/occupancy/')
        floor_row = response.context['blocks'][0]['floors'][0]
        self.assertEqual(floor_row['apartments'][0]['apartment'], self.apartment)
        self.assertEqual(floor_row['apartments'][0]['stats']['total'], 4)

    def test_the_rent_roll_names_the_apartment(self):
        guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'))
        self.assertContains(self.client.get('/reports/rent-roll/'), self.apartment.name)


class ApartmentScopingTests(ZyDwellTestCase):
    """Staff can be pinned to one apartment, not just a floor."""

    def setUp(self):
        super().setUp()
        self.other = Apartment.objects.create(
            tenant=self.tenant, floor=self.floor, name='102')
        other_room = Room.objects.create(
            tenant=self.tenant, apartment=self.other, number='201', capacity=2)
        self.other_bed = Bed.objects.create(
            tenant=self.tenant, room=other_room, label='A')

    def test_an_apartment_assignment_hides_the_rest_of_the_floor(self):
        from accounts.models import StaffAssignment
        from properties.models import visible_beds

        StaffAssignment.objects.create(user=self.staff, building=self.building,
                                       floor=self.floor, apartment=self.apartment)
        visible = visible_beds(self.staff, self.tenant)
        self.assertEqual(visible.count(), 4)
        self.assertNotIn(self.other_bed.pk, [bed.pk for bed in visible])

    def test_a_floor_assignment_still_covers_every_apartment_on_it(self):
        from accounts.models import StaffAssignment
        from properties.models import visible_beds

        StaffAssignment.objects.create(user=self.staff, building=self.building,
                                       floor=self.floor)
        self.assertEqual(visible_beds(self.staff, self.tenant).count(), 5)
