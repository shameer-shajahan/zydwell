"""After saving something new, the user lands back on the page they started from."""
from decimal import Decimal
from urllib.parse import quote

from django.utils import timezone

from billing.models import Payment, PaymentMode
from core.models import Status
from core.tests import ZyDwellTestCase
from guests import services as guest_services
from guests.models import Guest, Tenancy

CHECK_IN = '/guests/tenancies/check-in/'


class ReturnToPageTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username='owner', password='pw-owner-123')

    def assertSaved(self, response):
        form = response.context.get('form') if response.context else None
        if form is not None and form.errors:
            self.fail(f'the form was rejected: {form.errors.as_json()}')

    def new_guest(self, url, name='Walk In'):
        return self.client.post(url, {'full_name': name, 'mobile': '0550000000'})

    def check_in_payload(self, guest):
        return {
            'guest': guest.pk, 'room': self.room.pk, 'bed': self.beds[0].pk,
            'check_in_date': timezone.localdate(), 'monthly_rent': '800',
            'billing_cycle': 'monthly', 'billing_day': '1', 'rent_mode': 'advance',
            'first_charge': 'full', 'first_amount': '', 'deposit_amount': '0',
            'deposit_received': '0', 'agreement_note': '',
        }

    # --- the example from the request: check-in → new guest → check-in ----
    def test_check_in_offers_a_new_guest_that_comes_back(self):
        html = self.client.get(CHECK_IN).content.decode()
        self.assertIn(f'/guests/new/?next={quote(CHECK_IN, safe="")}&amp;pick=guest', html)

    def test_a_guest_made_from_check_in_returns_there_already_chosen(self):
        response = self.new_guest(f'/guests/new/?next={quote(CHECK_IN, safe="")}&pick=guest')
        guest = Guest.objects.get(full_name='Walk In')
        self.assertRedirects(response, f'{CHECK_IN}?guest={guest.pk}',
                             fetch_redirect_response=False)
        page = self.client.get(f'{CHECK_IN}?guest={guest.pk}')
        self.assertEqual(page.context['form']['guest'].value(), str(guest.pk))

    def test_the_chain_survives_a_check_in_opened_from_another_page(self):
        start = f'{CHECK_IN}?bed={self.beds[1].pk}&next=%2Fguests%2F'
        response = self.new_guest(f'/guests/new/?next={quote(start, safe="")}&pick=guest')
        guest = Guest.objects.get(full_name='Walk In')
        self.assertRedirects(
            response, f'{CHECK_IN}?bed={self.beds[1].pk}&next=%2Fguests%2F&guest={guest.pk}',
            fetch_redirect_response=False)

    def test_check_in_returns_to_the_room_it_started_from(self):
        room_page = f'/properties/rooms/{self.room.pk}/'
        response = self.client.post(f'{CHECK_IN}?next={quote(room_page, safe="")}',
                                    self.check_in_payload(self.guest))
        self.assertSaved(response)
        self.assertTrue(Tenancy.objects.filter(guest=self.guest).exists())
        self.assertRedirects(response, room_page, fetch_redirect_response=False)

    # --- nothing changes without a page to return to --------------------
    def test_without_next_a_guest_still_opens_their_own_page(self):
        response = self.new_guest('/guests/new/')
        guest = Guest.objects.get(full_name='Walk In')
        self.assertRedirects(response, f'/guests/{guest.pk}/', fetch_redirect_response=False)

    def test_a_link_to_another_site_is_ignored(self):
        for target in ('https://evil.example/', '//evil.example/', '/\\evil.example/'):
            Guest.objects.filter(full_name='Walk In').delete()
            response = self.new_guest(f'/guests/new/?next={quote(target, safe="")}')
            guest = Guest.objects.get(full_name='Walk In')
            self.assertRedirects(response, f'/guests/{guest.pk}/',
                                 fetch_redirect_response=False, msg_prefix=target)

    def test_a_return_without_pick_adds_nothing_to_the_page(self):
        # The bed board filters by ?bed=, so the new bed must not narrow it.
        board = '/properties/board/?building=%d' % self.building.pk
        self.room.capacity = 5  # the fixture room is already full at four beds
        self.room.save()
        response = self.client.post(f'/properties/beds/new/?next={quote(board, safe="")}', {
            'room': self.room.pk, 'label': 'E', 'monthly_price': '700',
            'status': Status.default_for(self.tenant, Status.KIND_BED).pk, 'notes': '',
        })
        self.assertRedirects(response, board, fetch_redirect_response=False)

    def test_cancel_goes_back_too(self):
        html = self.client.get(f'/guests/new/?next={quote(CHECK_IN, safe="")}').content.decode()
        self.assertIn(f'href="{CHECK_IN}"', html)
        html = self.client.get('/guests/new/').content.decode()
        self.assertIn('href="/guests/"', html)

    # --- money: save goes back, save-and-print still prints -------------
    def payment(self, url, **extra):
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'))
        data = {'tenancy': tenancy.pk, 'invoice': '', 'amount': '100',
                'mode': PaymentMode.by_code(self.tenant, 'cash').pk, 'reference': '',
                'paid_on': timezone.localdate(), 'note': '', **extra}
        return self.client.post(url, data), tenancy

    def test_a_payment_returns_to_the_tenancy(self):
        response, tenancy = self.payment('/billing/payments/new/?next=%2Fbilling%2Foverdue%2F')
        self.assertTrue(Payment.objects.filter(tenancy=tenancy).exists())
        self.assertRedirects(response, '/billing/overdue/', fetch_redirect_response=False)

    def test_save_and_print_still_opens_the_receipt(self):
        response, tenancy = self.payment('/billing/payments/new/?next=%2Fbilling%2Foverdue%2F',
                                         and_print='1')
        payment = Payment.objects.get(tenancy=tenancy)
        self.assertRedirects(response, f'/billing/payments/{payment.pk}/receipt/',
                             fetch_redirect_response=False)

    # --- an older hand-rolled check that let "//other.site" through ------
    def test_bed_status_will_not_bounce_to_another_site(self):
        status = Status.default_for(self.tenant, Status.KIND_BED)
        response = self.client.post(f'/properties/beds/{self.beds[2].pk}/status/',
                                    {'status': status.pk, 'next': '//evil.example/'})
        self.assertRedirects(response, f'/properties/rooms/{self.room.pk}/',
                             fetch_redirect_response=False)
