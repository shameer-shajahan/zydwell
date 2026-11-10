"""Capacity, uniqueness and the archive-instead-of-delete rule."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.utils import timezone

from core.tests import ZyDwellTestCase
from guests import services as guest_services
from properties.models import Apartment, Bed, Building, Floor, Room


class RoomAndBedRuleTests(ZyDwellTestCase):
    def test_beds_cannot_exceed_room_capacity(self):
        extra = Bed(tenant=self.tenant, room=self.room, label='E',
                    monthly_price=Decimal('800'))
        with self.assertRaises(ValidationError):
            extra.full_clean(exclude=['tenant', 'status'])

    def test_capacity_cannot_drop_below_the_beds_already_there(self):
        self.room.capacity = 2
        with self.assertRaises(ValidationError):
            self.room.full_clean(exclude=['tenant'])

    def test_a_bed_label_is_unique_inside_its_room(self):
        with self.assertRaises(IntegrityError):
            Bed.objects.create(tenant=self.tenant, room=self.room, label='A')

    def test_the_same_label_may_repeat_in_another_room(self):
        other = Room.objects.create(
            tenant=self.tenant, apartment=self.apartment, number='103', capacity=2)
        bed = Bed.objects.create(tenant=self.tenant, room=other, label='A')
        self.assertEqual(bed.room, other)

    def test_an_occupied_bed_cannot_be_deleted(self):
        guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'),
        )
        allowed, reason = self.beds[0].can_delete()
        self.assertFalse(allowed)
        self.assertIn('occupied', str(reason).lower())

    def test_a_free_bed_with_no_history_can_be_deleted(self):
        allowed, _reason = self.beds[3].can_delete()
        self.assertTrue(allowed)

    def test_the_delete_view_archives_instead_when_the_bed_is_in_use(self):
        guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'),
        )
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/properties/beds/{self.beds[0].pk}/delete/')
        self.beds[0].refresh_from_db()
        self.assertTrue(self.beds[0].is_archived)
        self.assertTrue(Bed.objects.filter(pk=self.beds[0].pk).exists())

    def test_a_room_in_use_is_archived_rather_than_deleted(self):
        guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'),
        )
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/properties/rooms/{self.room.pk}/delete/')
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_archived)

    def test_whole_unit_rooms_report_their_basis(self):
        self.assertTrue(self.flat.lets_whole_unit)
        self.assertFalse(self.room.lets_whole_unit)


class PlanLimitTests(ZyDwellTestCase):
    def test_the_building_limit_is_enforced_by_the_form(self):
        from properties.forms import BuildingForm

        self.tenant.max_buildings = 1
        self.tenant.save()
        form = BuildingForm(data={'name': 'Overflow Tower'}, tenant=self.tenant)
        self.assertFalse(form.is_valid())
        self.assertIn('1 buildings', str(form.errors))

    def test_the_bed_limit_is_enforced_by_the_form(self):
        from properties.forms import BedForm

        self.tenant.max_beds = 4
        self.tenant.save()
        form = BedForm(
            data={'room': self.flat.pk, 'label': 'A', 'monthly_price': '100'},
            tenant=self.tenant,
        )
        self.assertFalse(form.is_valid())


class DeleteHierarchyTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username='owner', password='pw-owner-123')

    def test_an_empty_building_is_deleted(self):
        empty = Building.objects.create(tenant=self.tenant, name='Empty Block')
        self.client.post(f'/properties/{empty.pk}/delete/')
        self.assertFalse(Building.objects.filter(pk=empty.pk).exists())

    def test_an_empty_floor_is_deleted(self):
        empty = Floor.objects.create(tenant=self.tenant, building=self.building, name='Roof')
        self.client.post(f'/properties/floors/{empty.pk}/delete/')
        self.assertFalse(Floor.objects.filter(pk=empty.pk).exists())

    def test_a_building_with_floors_is_archived_instead(self):
        self.client.post(f'/properties/{self.building.pk}/delete/')
        self.building.refresh_from_db()
        self.assertTrue(self.building.is_archived)

    def test_a_floor_with_a_live_guest_is_archived_instead(self):
        guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'),
        )
        self.client.post(f'/properties/floors/{self.floor.pk}/delete/')
        self.floor.refresh_from_db()
        self.assertTrue(self.floor.is_archived)

    def test_a_room_with_only_past_stays_is_archived_not_crashed(self):
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'),
        )
        guest_services.check_out(tenancy=tenancy, user=self.owner)
        Bed.objects.filter(room=self.room).update(is_archived=True)
        response = self.client.post(f'/properties/rooms/{self.room.pk}/delete/')
        self.assertEqual(response.status_code, 302)
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_archived)

    def test_staff_without_delete_permission_cannot_delete(self):
        empty = Building.objects.create(tenant=self.tenant, name='Empty Block')
        self.grant('properties', can_create=True, can_edit=True)
        self.client.login(username='staff', password='pw-staff-123')
        response = self.client.post(f'/properties/{empty.pk}/delete/')
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Building.objects.filter(pk=empty.pk).exists())

    def test_the_building_page_shows_delete_buttons_to_the_owner(self):
        response = self.client.get(f'/properties/{self.building.pk}/')
        self.assertContains(response, f'/properties/{self.building.pk}/delete/')
        self.assertContains(response, f'/properties/floors/{self.floor.pk}/delete/')
        self.assertContains(response, f'/properties/apartments/{self.apartment.pk}/delete/')
