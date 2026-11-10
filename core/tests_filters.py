"""The Building → Floor → Apartment → Room → Bed picker, lists that filter and
search across every page rather than the one on screen, lists that cost the
same number of queries however long they grow, and the WhatsApp log's
deletion and 30-day clean-up."""
from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.template import Context, Template
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import StaffAssignment
from billing.models import Invoice
from core.constants import (
    MODULE_BILLING, MODULE_GUESTS, MODULE_MESSAGING, MODULE_PROPERTIES,
)
from core.filters import LocationFilter
from core.models import AuditLog, Status, Tenant
from core.seed import seed_tenant
from core.tests import ZyDwellTestCase
from guests.models import Guest, Tenancy
from messaging.models import MessageLog
from properties.models import Apartment, Bed, Building, Floor, Room


class PlacesTestCase(ZyDwellTestCase):
    """Adds a second apartment on the same floor and a second building."""

    def setUp(self):
        super().setUp()
        self.flat_b = Apartment.objects.create(tenant=self.tenant, floor=self.floor, name='102')
        self.room_b = Room.objects.create(
            tenant=self.tenant, apartment=self.flat_b, number='201', capacity=80)
        self.tower_b = Building.objects.create(tenant=self.tenant, name='West Tower')
        floor = Floor.objects.create(tenant=self.tenant, building=self.tower_b, name='G')
        flat = Apartment.objects.create(tenant=self.tenant, floor=floor, name='G1')
        self.room_west = Room.objects.create(
            tenant=self.tenant, apartment=flat, number='1', capacity=80)
        self.guest_count = 0

    def stay(self, room, name=None, days_ago=10, rent='800'):
        """One live tenancy on a new bed in ``room``."""
        self.guest_count += 1
        bed = Bed.objects.create(tenant=self.tenant, room=room,
                                 label=f'X{self.guest_count}', monthly_price=Decimal(rent))
        guest = Guest.objects.create(tenant=self.tenant, mobile=f'05{self.guest_count:08d}',
                                     full_name=name or f'Guest {self.guest_count:03d}')
        return Tenancy.objects.create(
            tenant=self.tenant, guest=guest, room=room, bed=bed,
            check_in_date=timezone.localdate() - timedelta(days=days_ago),
            monthly_rent=Decimal(rent))

    def owed(self, tenancy, days_late=10, amount='500'):
        today = timezone.localdate()
        return Invoice.objects.create(
            tenant=self.tenant, tenancy=tenancy, period_start=today - timedelta(days=30),
            period_end=today, due_date=today - timedelta(days=days_late),
            rent_amount=Decimal(amount), total=Decimal(amount),
            status=Status.by_code(self.tenant, Status.KIND_INVOICE, 'overdue'))

    def picker(self, user=None, **params):
        request = RequestFactory().get('/', params)
        request.user = user or self.owner
        request.tenant = self.tenant
        return LocationFilter(request)


class LocationFilterTests(PlacesTestCase):
    def field(self, location, name):
        return next(f for f in location.fields if f['name'] == name)

    def test_with_two_buildings_only_the_building_level_is_open(self):
        location = self.picker()
        self.assertFalse(location.active)
        self.assertEqual([text for _, text in self.field(location, 'building')['options']],
                         ['Test Tower', 'West Tower'])
        for level in ('floor', 'apartment', 'room', 'bed'):
            self.assertTrue(self.field(location, level)['disabled'], level)

    def test_each_choice_opens_the_next_level(self):
        location = self.picker(building=self.building.pk)
        floors = self.field(location, 'floor')
        self.assertFalse(floors['disabled'])
        self.assertEqual(floors['options'], [(str(self.floor.pk), 'Floor 1')])
        # One floor means nothing to choose, so the apartments open as well.
        flats = self.field(location, 'apartment')
        self.assertEqual([text for _, text in flats['options']], ['101', '102'])
        self.assertTrue(self.field(location, 'room')['disabled'])

    def test_a_bed_alone_fills_in_everything_above_it(self):
        location = self.picker(bed=self.beds[2].pk)
        self.assertEqual(location.chain, {
            'building': self.building, 'floor': self.floor, 'apartment': self.apartment,
            'room': self.room, 'bed': self.beds[2]})
        self.assertEqual(location.deepest, ('bed', self.beds[2]))

    def test_a_changed_building_drops_the_old_floor_and_below(self):
        location = self.picker(building=self.tower_b.pk, floor=self.floor.pk,
                               apartment=self.apartment.pk)
        self.assertEqual(location.chain, {'building': self.tower_b})

    def test_a_changed_floor_keeps_the_building(self):
        other_floor = Floor.objects.create(tenant=self.tenant, building=self.building, name='2')
        location = self.picker(building=self.building.pk, floor=other_floor.pk,
                               apartment=self.apartment.pk)
        self.assertEqual(location.chain, {'building': self.building, 'floor': other_floor})

    def test_rubbish_and_other_clients_ids_are_ignored(self):
        stranger = Tenant.objects.create(name='Someone Else', slug='someone-else')
        seed_tenant(stranger, with_subscription=False)
        theirs = Building.objects.create(tenant=stranger, name='Not Yours')
        self.assertFalse(self.picker(building=theirs.pk).active)
        self.assertFalse(self.picker(building='1 OR 1=1', room='abc').active)

    def test_archived_places_are_not_offered(self):
        self.flat_b.is_archived = True
        self.flat_b.save()
        location = self.picker(building=self.building.pk)
        self.assertEqual([text for _, text in self.field(location, 'apartment')['options']],
                         ['101'])
        self.assertFalse(self.picker(apartment=self.flat_b.pk).active)

    def test_staff_are_offered_only_the_places_they_look_after(self):
        StaffAssignment.objects.create(user=self.staff, building=self.building,
                                       floor=self.floor, apartment=self.apartment)
        location = self.picker(user=self.staff)
        self.assertEqual([text for _, text in self.field(location, 'building')['options']],
                         ['Test Tower'])
        self.assertEqual([text for _, text in self.field(location, 'apartment')['options']],
                         ['101'])
        self.assertFalse(self.picker(user=self.staff, apartment=self.flat_b.pk).active)
        self.assertFalse(self.picker(user=self.staff, building=self.tower_b.pk).active)

    def test_rooms_and_beds_sort_like_people_count(self):
        for label in ('E10', 'E2', 'E1'):
            Bed.objects.create(tenant=self.tenant, room=self.room_b, label=label)
        location = self.picker(room=self.room_b.pk)
        self.assertEqual([text for _, text in self.field(location, 'bed')['options']],
                         ['E1', 'E2', 'E10'])


class ListsFilterEveryRowTests(PlacesTestCase):
    """Filtering and searching look at every row, not just the page on screen."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_tenancies_filter_by_apartment_across_every_page(self):
        for _ in range(27):                     # more than one page of 25
            self.stay(self.room)
        for _ in range(3):
            self.stay(self.room_b)
        page = self.client.get('/guests/tenancies/', {'apartment': self.flat_b.pk}
                               ).context['page']
        self.assertEqual(page.paginator.count, 3)
        page = self.client.get('/guests/tenancies/', {'apartment': self.apartment.pk}
                               ).context['page']
        self.assertEqual(page.paginator.count, 27)
        self.assertEqual(self.client.get('/guests/tenancies/', {'bed': page[0].bed_id}
                                         ).context['page'].paginator.count, 1)

    def test_rent_roll_search_reaches_past_the_first_page(self):
        for _ in range(50):
            self.stay(self.room)
        # The oldest check-in sorts last, onto page two.
        self.stay(self.room, name='Zafar Hidden', days_ago=400)
        first = self.client.get('/reports/rent-roll/').context['data']
        self.assertNotIn('Zafar Hidden', [t.guest.full_name for t in first])
        found = self.client.get('/reports/rent-roll/', {'q': 'zafar'}).context['data']
        self.assertEqual([t.guest.full_name for t in found], ['Zafar Hidden'])

    def test_expiring_documents_search_the_server_not_the_page(self):
        guest = self.stay(self.room).guest
        guest.documents.create(tenant=self.tenant, doc_type='visa', number='VZ-777',
                               expiry_date=timezone.localdate() + timedelta(days=5))
        response = self.client.get('/guests/documents/expiring/', {'q': 'vz-777'})
        self.assertEqual(response.context['page'].paginator.count, 1)
        self.assertNotContains(response, 'data-filter="tbody tr"')

    def test_guest_list_combines_place_and_state(self):
        here = self.stay(self.room_b, name='In Flat B')
        self.stay(self.room, name='In Flat A')
        gone = self.stay(self.room_b, name='Left Flat B')
        gone.status = Status.by_code(self.tenant, Status.KIND_TENANCY, 'vacated')
        gone.save()
        names = lambda **params: [card['obj'].full_name for card in self.client.get(
            '/guests/', params).context['cards']]
        self.assertEqual(names(apartment=self.flat_b.pk), ['In Flat B', 'Left Flat B'])
        self.assertEqual(names(apartment=self.flat_b.pk, state='current'), ['In Flat B'])
        self.assertEqual(names(apartment=self.flat_b.pk, state='past'), ['Left Flat B'])
        self.assertEqual(names(state='current'), ['In Flat A', 'In Flat B'])
        self.assertTrue(here.is_live)

    def test_every_list_screen_takes_the_location_picker(self):
        tenancy = self.stay(self.room_b)
        self.owed(tenancy)
        # Every level, so screens that stop at the room or apartment still pick one.
        params = {'from': '2000-01-01', 'to': '2100-01-01', 'building': self.building.pk,
                  'floor': self.floor.pk, 'apartment': self.flat_b.pk,
                  'room': self.room_b.pk, 'bed': tenancy.bed_id}
        for url in ['/guests/', '/guests/tenancies/', '/guests/documents/expiring/',
                    '/billing/invoices/', '/billing/payments/', '/billing/overdue/',
                    '/properties/board/', '/messaging/', '/messaging/waiting/',
                    '/accounting/', '/reports/rent-roll/', '/reports/collection/',
                    '/reports/expense/', '/reports/profit/', '/reports/deposits/',
                    '/reports/occupancy/']:
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 200, url)
            location = response.context['location']
            self.assertTrue(location.active, url)
            self.assertContains(response, 'data-loc-level="0"', msg_prefix=url)

    def test_overdue_is_narrowed_to_the_place(self):
        self.owed(self.stay(self.room), amount='300')
        self.owed(self.stay(self.room_west), amount='700')
        response = self.client.get('/billing/overdue/', {'building': self.tower_b.pk})
        self.assertEqual(response.context['total_due'], Decimal('700'))
        self.assertEqual(response.context['buckets']['0-30'], Decimal('700'))

    def test_the_tabs_keep_the_place_and_search(self):
        response = self.client.get('/guests/tenancies/', {
            'apartment': self.flat_b.pk, 'q': 'abc', 'page': 3})
        html = response.content.decode()
        self.assertIn('state=vacated', html)
        self.assertIn(f'apartment={self.flat_b.pk}', html)
        self.assertNotIn('page=3', html)

    def test_the_filters_sit_in_one_compact_row(self):
        response = self.client.get('/billing/payments/', {'building': self.building.pk})
        html = response.content.decode()
        self.assertEqual(html.count('data-fb-scroll'), 1)
        # Each pill shows its own name until something is chosen, then "Floor 1".
        self.assertIn('data-placeholder="Floor" data-prefix="Floor"', html)
        self.assertIn('data-placeholder="Mode" data-prefix="Mode"', html)
        self.assertIn(f'<option value="{self.building.pk}" selected>Test Tower</option>', html)
        self.assertIn('class="loc-clear"', html)
        # A level that cannot be used yet says why.
        self.assertIn('disabled title="Room first"', html)

    def test_nothing_chosen_means_nothing_to_clear(self):
        self.assertNotContains(self.client.get('/guests/tenancies/'), 'class="loc-clear"')

    def test_exports_follow_the_picked_place(self):
        self.stay(self.room, name='Stays East')
        self.stay(self.room_west, name='Stays West')
        response = self.client.get('/export/rent-roll/', {'building': self.tower_b.pk})
        from openpyxl import load_workbook

        sheet = load_workbook(BytesIO(response.content)).active
        names = [row[0].value for row in sheet.iter_rows(min_row=2)]
        self.assertIn('Stays West', names)
        self.assertNotIn('Stays East', names)


class QueryCountTests(PlacesTestCase):
    """A list costs the same queries for 3 rows or 12 — no query per row."""

    URLS = ['/guests/', '/guests/tenancies/', '/billing/invoices/', '/billing/payments/',
            '/billing/overdue/', '/properties/', '/properties/board/', '/messaging/',
            '/accounting/', '/reports/rent-roll/', '/reports/occupancy/', '/']

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def grow(self, count):
        for index in range(count):
            room = (self.room_b, self.room_west)[index % 2]
            tenancy = self.stay(room)
            self.owed(tenancy)
            tenancy.guest.documents.create(
                tenant=self.tenant, doc_type='visa', number=f'V{tenancy.pk}',
                expiry_date=timezone.localdate() + timedelta(days=9))
            MessageLog.objects.create(tenant=self.tenant, guest=tenancy.guest,
                                      tenancy=tenancy, to_number='0500000000', body='hi',
                                      status=MessageLog.STATUS_SENT)

    def counts(self):
        cache.clear()
        out = {}
        for url in self.URLS:
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(url).status_code, 200, url)
            out[url] = len(queries)
        return out

    def test_lists_do_not_query_once_per_row(self):
        self.grow(3)
        few = self.counts()
        self.grow(9)
        many = self.counts()
        for url in self.URLS:
            self.assertEqual(few[url], many[url], f'{url}: {few[url]} → {many[url]} queries')

    def test_nor_does_the_staff_dashboard(self):
        for module in (MODULE_PROPERTIES, MODULE_GUESTS, MODULE_BILLING, MODULE_MESSAGING):
            self.grant(module)
        self.client.force_login(self.staff)
        self.URLS = ['/']
        self.grow(3)
        few = self.counts()['/']
        self.grow(9)
        self.assertEqual(few, self.counts()['/'])


class WhatsAppLogCleanupTests(PlacesTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client.force_login(self.owner)

    def message(self, days_old, status=MessageLog.STATUS_SENT):
        entry = MessageLog.objects.create(tenant=self.tenant, to_number='0501112222',
                                          body='Rent reminder', status=status)
        MessageLog.objects.filter(pk=entry.pk).update(
            created_at=timezone.now() - timedelta(days=days_old))
        return entry

    def test_entries_past_thirty_days_go_but_waiting_ones_stay(self):
        old = self.message(31)
        old_failed = self.message(45, MessageLog.STATUS_FAILED)
        old_waiting = self.message(60, MessageLog.STATUS_QUEUED)
        recent = self.message(29)
        from messaging.services import purge_old_messages

        self.assertEqual(purge_old_messages(self.tenant), 2)
        left = set(MessageLog.objects.values_list('pk', flat=True))
        self.assertEqual(left, {old_waiting.pk, recent.pk})
        self.assertNotIn(old.pk, left)
        self.assertNotIn(old_failed.pk, left)
        self.assertTrue(AuditLog.objects.filter(
            tenant=self.tenant, model_name='MessageLog',
            summary__icontains='older than 30 days').exists())

    def test_opening_the_log_cleans_up_once_a_day(self):
        self.message(40)
        self.client.get('/messaging/')
        self.assertFalse(MessageLog.objects.exists())
        self.message(40)
        self.client.get('/messaging/')          # already ran today
        self.assertEqual(MessageLog.objects.count(), 1)

    def test_the_nightly_job_cleans_up_too(self):
        self.message(40)
        keep = self.message(2)
        call_command('run_daily_jobs', tenant=self.tenant.slug, stdout=StringIO())
        self.assertEqual(list(MessageLog.objects.values_list('pk', flat=True)), [keep.pk])

    def test_an_owner_can_delete_one_entry(self):
        entry, other = self.message(1), self.message(1)
        response = self.client.post(f'/messaging/{entry.pk}/delete/',
                                    {'next': '/messaging/?status=sent'})
        self.assertRedirects(response, '/messaging/?status=sent', fetch_redirect_response=False)
        self.assertEqual(list(MessageLog.objects.values_list('pk', flat=True)), [other.pk])
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.ACTION_DELETE,
                                                model_name='MessageLog').exists())

    def test_the_page_shows_delete_only_to_those_allowed(self):
        button = f'/messaging/{self.message(1).pk}/delete/'
        self.assertContains(self.client.get('/messaging/'), button)
        self.grant(MODULE_MESSAGING)
        self.client.force_login(self.staff)
        self.assertNotContains(self.client.get('/messaging/'), button)

    def test_staff_without_the_delete_grant_cannot(self):
        entry = self.message(1)
        self.grant(MODULE_MESSAGING)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(f'/messaging/{entry.pk}/delete/').status_code, 403)
        self.assertTrue(MessageLog.objects.filter(pk=entry.pk).exists())

    def test_a_get_never_deletes(self):
        entry = self.message(1)
        self.client.get(f'/messaging/{entry.pk}/delete/')
        self.assertTrue(MessageLog.objects.filter(pk=entry.pk).exists())

    def test_it_will_not_bounce_to_another_site(self):
        entry = self.message(1)
        response = self.client.post(f'/messaging/{entry.pk}/delete/',
                                    {'next': 'https://evil.example/'})
        self.assertRedirects(response, '/messaging/', fetch_redirect_response=False)


class QueryTagTests(ZyDwellTestCase):
    def render(self, source, path):
        request = RequestFactory().get(path)
        return Template('{% load listing %}' + source).render(Context({'request': request}))

    def test_changes_one_value_and_restarts_paging(self):
        out = self.render("{% query state='past' %}", '/?q=ali&state=current&page=4&invoice_page=2')
        self.assertEqual(out, '?q=ali&amp;state=past')

    def test_location_clears_every_level(self):
        out = self.render("{% query 'location' %}", '/?q=x&building=1&floor=2&room=3&bed=4')
        self.assertEqual(out, '?q=x')

    def test_an_empty_value_removes_the_parameter(self):
        self.assertEqual(self.render("{% query state='' %}", '/?state=past'), '?')
