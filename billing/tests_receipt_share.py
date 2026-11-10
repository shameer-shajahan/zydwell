"""Sending a guest their receipt. WhatsApp carries text, so the text carries a link."""
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlparse

from django.core import signing
from django.test import override_settings
from django.utils import timezone

from billing import services, sharing
from billing.models import Payment
from core.models import AuditLog
from core.tests import ZyDwellTestCase
from guests import services as guest_services
from messaging.models import MessageLog

PHONE = 'iPhone; CPU iPhone OS 17_0 like Mac OS X'


class ReceiptShareTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'),
        )
        self.invoice = services.build_invoice(
            self.tenancy, period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29), user=self.owner)
        self.payment = services.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
            amount=Decimal('900'), invoice=self.invoice)
        self.client.login(username='owner', password='pw-owner-123')

    # --- the signed link ------------------------------------------------
    def test_a_guest_can_read_their_receipt_without_an_account(self):
        self.client.logout()
        response = self.client.get(sharing.receipt_url(self.payment))
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response['Content-Type'])
        html = response.content.decode()
        for needle in [self.payment.receipt_number, self.guest.full_name,
                       self.building.name, '900.00']:
            self.assertIn(needle, html)

    def test_the_guest_is_given_nothing_to_download_or_change(self):
        html = self.client.get(sharing.receipt_url(self.payment)).content.decode()
        self.assertNotIn('<form', html.lower())
        self.assertNotIn('.pdf', html.lower())
        self.assertNotIn('<input', html.lower())

    def test_the_link_is_not_indexed(self):
        response = self.client.get(sharing.receipt_url(self.payment))
        self.assertIn('noindex', response['X-Robots-Tag'])

    def test_a_tampered_token_is_refused(self):
        token = sharing.receipt_token(self.payment)
        response = self.client.get(f'/r/{token[:-4]}xxxx/')
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, self.guest.full_name, status_code=404)

    def test_an_old_link_stops_working(self):
        token = sharing.receipt_token(self.payment)
        with override_settings(ZYDWELL={**self.settings_dict(), 'RECEIPT_LINK_DAYS': 0}):
            response = self.client.get(f'/r/{token}/')
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, 'expired', status_code=410)

    def settings_dict(self):
        from django.conf import settings

        return dict(settings.ZYDWELL)

    def test_a_token_cannot_be_reused_for_another_purpose(self):
        """A different salt means a guest-profile token is useless here."""
        foreign = signing.dumps({'p': self.payment.pk, 't': self.tenant.pk},
                                salt='something.else')
        self.assertEqual(self.client.get(f'/r/{foreign}/').status_code, 404)

    def test_it_only_finds_a_payment_in_its_own_tenant(self):
        from core.models import Tenant

        other = Tenant.objects.create(name='Other', slug='other-acc')
        token = signing.dumps({'p': self.payment.pk, 't': other.pk}, salt=sharing.SALT)
        payment, problem = sharing.payment_from_token(token)
        self.assertIsNone(payment)
        self.assertEqual(problem, 'invalid')

    # --- the WhatsApp hand-off -------------------------------------------
    def test_sending_opens_whatsapp_with_the_link_in_the_text(self):
        response = self.client.get(
            f'/billing/payments/{self.payment.pk}/receipt/whatsapp/',
            HTTP_USER_AGENT=PHONE)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('https://wa.me/'))

        text = unquote(parse_qs(urlparse(response['Location']).query)['text'][0])
        self.assertIn(self.payment.receipt_number, text)
        self.assertIn('900.00', text)
        self.assertIn('/r/', text)

    def test_the_link_in_the_message_actually_opens_the_receipt(self):
        response = self.client.get(
            f'/billing/payments/{self.payment.pk}/receipt/whatsapp/',
            HTTP_USER_AGENT=PHONE)
        text = unquote(parse_qs(urlparse(response['Location']).query)['text'][0])
        path = '/r/' + text.split('/r/')[1].split()[0].rstrip('.')

        self.client.logout()
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.payment.receipt_number, page.content.decode())

    def test_it_is_logged_and_audited(self):
        self.client.get(f'/billing/payments/{self.payment.pk}/receipt/whatsapp/')
        entry = MessageLog.objects.get(template_code='receipt_share')
        self.assertEqual(entry.status, MessageLog.STATUS_OPENED)
        self.assertEqual(entry.sent_by, self.owner)
        self.assertTrue(AuditLog.objects.filter(
            action=AuditLog.ACTION_PRINT, summary__contains='WhatsApp').exists())

    def test_a_guest_with_no_number_is_told_rather_than_redirected(self):
        self.guest.mobile = ''
        self.guest.whatsapp_number = ''
        self.guest.save()
        response = self.client.get(
            f'/billing/payments/{self.payment.pk}/receipt/whatsapp/', follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MessageLog.objects.get().status, MessageLog.STATUS_FAILED)

    def test_the_staff_page_offers_the_link_and_no_pdf(self):
        html = self.client.get(
            f'/billing/payments/{self.payment.pk}/receipt/').content.decode()
        self.assertIn('receipt-url', html)           # copyable link
        self.assertIn('/receipt/whatsapp/', html)
        self.assertNotIn('data-share-pdf', html)
        self.assertNotIn('receipt/pdf', html)

    def test_staff_can_only_share_receipts_they_can_see(self):
        from properties.models import Building, Floor, Room
        from accounts.models import StaffAssignment

        other_building = Building.objects.create(tenant=self.tenant, name='Far Tower')
        StaffAssignment.objects.create(user=self.staff, building=other_building)
        self.grant('billing', can_view=True, can_create=True)
        self.client.logout()
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get(
            f'/billing/payments/{self.payment.pk}/receipt/whatsapp/').status_code, 404)
