"""What a guest holding a link can reach — and everything they cannot."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from billing import services as billing
from billing.models import Payment
from core.tests import ZyDwellTestCase
from guests import services
from guests.models import DepositTransaction, Guest, GuestInvite


class GuestPortalTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=30),
            monthly_rent=Decimal('900'), deposit_amount=Decimal('900'),
            deposit_received=Decimal('900'),
        )
        invoice = billing.build_invoice(
            self.tenancy, period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29), user=self.owner)
        self.payment = billing.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
            amount=Decimal('900'), invoice=invoice)
        self.deposit = self.tenancy.deposit_transactions.get()
        self.invite = GuestInvite.issue(guest=self.guest, user=self.owner)
        self.url = f'/g/{self.invite.token}'

        # A second guest, whose data must stay invisible.
        self.other = Guest.objects.create(tenant=self.tenant, full_name='Other Person',
                                          mobile='0509999999')
        other_tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.other, room=self.room,
            bed=self.beds[1], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('700'), deposit_amount=Decimal('700'),
            deposit_received=Decimal('700'))
        self.other_payment = billing.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=other_tenancy,
            amount=Decimal('700'))
        self.other_deposit = other_tenancy.deposit_transactions.get()

    # --- what they can reach --------------------------------------------
    def test_the_home_page_greets_them_and_offers_two_things(self):
        response = self.client.get(f'{self.url}/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(self.guest.full_name, html)
        self.assertIn('Your details', html)
        self.assertIn('Your receipts', html)
        self.assertEqual(response.context['receipt_count'], 2)

    def test_they_can_edit_their_own_details(self):
        response = self.client.post(f'{self.url}/details/', {
            'full_name': 'Renamed Themselves', 'mobile': '0551112233',
            'whatsapp_number': '0551112233', 'gender': 'female',
            'passport_number': 'P7654321', 'nationality': 'Indian',
        })
        self.assertContains(response, 'Thank you')
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.full_name, 'Renamed Themselves')

    def test_they_can_list_their_own_receipts(self):
        response = self.client.get(f'{self.url}/receipts/')
        self.assertEqual(response.status_code, 200)
        numbers = [row['number'] for row in response.context['rows']]
        self.assertIn(self.payment.receipt_number, numbers)
        self.assertIn(self.deposit.receipt_number, numbers)
        self.assertEqual(len(numbers), 2)

    def test_they_can_read_their_own_receipts(self):
        for kind, pk, number in [('rent', self.payment.pk, self.payment.receipt_number),
                                 ('deposit', self.deposit.pk,
                                  self.deposit.receipt_number)]:
            response = self.client.get(f'{self.url}/receipts/{kind}/{pk}/')
            self.assertEqual(response.status_code, 200, kind)
            self.assertIn('text/html', response['Content-Type'])
            self.assertIn(number, response.content.decode())

    def test_a_receipt_is_read_only_with_nothing_to_download(self):
        html = self.client.get(
            f'{self.url}/receipts/rent/{self.payment.pk}/').content.decode()
        self.assertNotIn('<form', html.lower())
        self.assertNotIn('.pdf', html.lower())

    # --- what they cannot reach ------------------------------------------
    def test_another_guest_receipt_reads_as_missing(self):
        """Not 403 — a refusal would confirm the receipt exists."""
        for kind, pk in [('rent', self.other_payment.pk),
                         ('deposit', self.other_deposit.pk)]:
            response = self.client.get(f'{self.url}/receipts/{kind}/{pk}/')
            self.assertEqual(response.status_code, 404, kind)

    def test_no_other_guest_appears_anywhere_in_the_portal(self):
        for path in ['/', '/details/', '/receipts/']:
            html = self.client.get(f'{self.url}{path}').content.decode()
            self.assertNotIn(self.other.full_name, html, path)
            self.assertNotIn(self.other_payment.receipt_number, html, path)

    def test_a_reversed_receipt_is_not_offered(self):
        billing.reverse_payment(payment=self.payment, user=self.owner, reason='Bounced')
        rows = self.client.get(f'{self.url}/receipts/').context['rows']
        self.assertNotIn(self.payment.receipt_number, [r['number'] for r in rows])
        self.assertEqual(
            self.client.get(f'{self.url}/receipts/rent/{self.payment.pk}/').status_code, 404)

    def test_the_portal_shows_no_money_they_are_not_owed_an_answer_about(self):
        """Invoices, balances and the office's own figures stay out of it."""
        for path in ['/', '/details/', '/receipts/']:
            html = self.client.get(f'{self.url}{path}').content.decode()
            for leak in ['Outstanding', 'Pending rent', 'Net profit', 'Occupancy',
                         'Dashboard', 'Invoice', 'Staff']:
                self.assertNotIn(leak, html, f'{path} leaked "{leak}"')

    def test_the_portal_offers_no_way_into_the_office(self):
        html = self.client.get(f'{self.url}/').content.decode()
        for route in ['/billing/', '/reports/', '/accounting/', '/properties/',
                      '/accounts/', '/platform/', '/guests/']:
            self.assertNotIn(f'href="{route}', html, route)

    def test_a_guest_cannot_reach_the_office_even_by_typing_the_address(self):
        for path in ['/', '/guests/', '/billing/invoices/', '/reports/',
                     '/settings/', '/platform/']:
            response = self.client.get(path)
            self.assertIn(response.status_code, (302, 403), path)

    def test_a_dead_link_gives_nothing_away(self):
        self.invite.revoked_at = timezone.now()
        self.invite.save()
        for path in ['/', '/details/', '/receipts/',
                     f'/receipts/rent/{self.payment.pk}/']:
            response = self.client.get(f'{self.url}{path}')
            self.assertEqual(response.status_code, 410, path)
            self.assertNotIn(self.guest.full_name, response.content.decode())

    def test_an_expired_link_stops_the_whole_portal(self):
        self.invite.expires_at = timezone.now() - timedelta(minutes=1)
        self.invite.save()
        self.assertEqual(self.client.get(f'{self.url}/receipts/').status_code, 410)

    def test_a_made_up_token_gives_nothing_away(self):
        response = self.client.get('/g/not-a-real-token/receipts/')
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(self.guest.full_name, response.content.decode())

    def test_a_guest_cannot_edit_anything_the_office_owns(self):
        """Rent, bed, deposit and blacklisting are not on their form."""
        from guests.forms import GuestSelfProfileForm

        forbidden = {'monthly_rent', 'deposit_amount', 'bed', 'room', 'is_blacklisted',
                     'blacklist_reason', 'notes', 'rent_mode', 'first_invoice_amount'}
        self.assertEqual(forbidden & set(GuestSelfProfileForm.Meta.fields), set())

    def test_posting_office_fields_changes_nothing(self):
        self.client.post(f'{self.url}/details/', {
            'full_name': self.guest.full_name,
            'is_blacklisted': 'on', 'notes': 'injected', 'monthly_rent': '1',
        })
        self.guest.refresh_from_db()
        self.tenancy.refresh_from_db()
        self.assertFalse(self.guest.is_blacklisted)
        self.assertEqual(self.guest.notes, '')
        self.assertEqual(self.tenancy.monthly_rent, Decimal('900'))

    def test_receipts_are_not_indexed(self):
        response = self.client.get(f'{self.url}/receipts/rent/{self.payment.pk}/')
        self.assertIn('noindex', response['X-Robots-Tag'])
        html = self.client.get(f'{self.url}/').content.decode()
        self.assertIn('noindex', html)
