"""Opening WhatsApp from the system, with no Business API involved."""
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from django.utils import timezone

from billing import services as billing
from core.models import AuditLog
from core.tests import ZyDwellTestCase
from guests import services as guest_services
from messaging import services
from messaging.models import CHANNEL_CLICK, MessageLog, MessageTemplate

PHONE = 'iPhone; CPU iPhone OS 17_0 like Mac OS X'
LAPTOP = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'


class ChatUrlTests(ZyDwellTestCase):
    def test_the_number_is_normalised_into_the_link(self):
        url = services.chat_url('0501234567', 'hello')
        self.assertTrue(url.startswith('https://wa.me/971501234567?text='))

    def test_the_message_is_url_encoded(self):
        url = services.chat_url('0501234567', 'Rent AED 800 & fees due 01/10')
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query['text'][0], 'Rent AED 800 & fees due 01/10')
        self.assertNotIn(' ', url)

    def test_desktop_goes_to_whatsapp_web(self):
        url = services.chat_url('0501234567', 'hi', prefer_web=True)
        self.assertTrue(url.startswith('https://web.whatsapp.com/send?phone=971501234567'))

    def test_no_number_means_no_link(self):
        self.assertEqual(services.chat_url('', 'hi'), '')

    def test_the_device_decides_which_form_to_use(self):
        class FakeRequest:
            def __init__(self, agent):
                self.META = {'HTTP_USER_AGENT': agent}

        self.assertFalse(services.prefers_whatsapp_web(FakeRequest(PHONE)))
        self.assertTrue(services.prefers_whatsapp_web(FakeRequest(LAPTOP)))
        self.assertFalse(services.prefers_whatsapp_web(None))


class QuickSendTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=20),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        self.invoice = billing.build_invoice(
            self.tenancy, period_start=timezone.localdate() - timedelta(days=10),
            period_end=timezone.localdate() + timedelta(days=19),
            due_date=timezone.localdate() - timedelta(days=10), user=self.owner)
        self.client.login(username='owner', password='pw-owner-123')

    def test_one_tap_logs_the_message_and_opens_whatsapp(self):
        response = self.client.get(
            f'/messaging/open/?guest={self.guest.pk}&invoice={self.invoice.pk}'
            '&template=rent_overdue', HTTP_USER_AGENT=PHONE)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('https://wa.me/971501234567?text='))

        entry = MessageLog.objects.get()
        self.assertEqual(entry.channel, CHANNEL_CLICK)
        self.assertEqual(entry.status, MessageLog.STATUS_OPENED)
        self.assertEqual(entry.sent_by, self.owner)
        self.assertIn(self.guest.full_name, entry.body)

    def test_the_link_carries_the_real_figures(self):
        response = self.client.get(
            f'/messaging/open/?guest={self.guest.pk}&invoice={self.invoice.pk}'
            '&template=rent_overdue', HTTP_USER_AGENT=PHONE)
        text = parse_qs(urlparse(response['Location']).query)['text'][0]
        self.assertIn(str(self.invoice.number), text)
        self.assertIn('900', text)

    def test_it_does_not_need_an_approved_template(self):
        """Meta approval gates the API, not a staff member typing in WhatsApp."""
        MessageTemplate.objects.filter(tenant=self.tenant).update(is_approved=False)
        response = self.client.get(
            f'/messaging/open/?guest={self.guest.pk}&template=rent_reminder',
            HTTP_USER_AGENT=PHONE)
        self.assertEqual(response.status_code, 302)
        self.assertIn('wa.me', response['Location'])
        self.assertEqual(MessageLog.objects.get().status, MessageLog.STATUS_OPENED)

    def test_a_desktop_is_sent_to_whatsapp_web(self):
        response = self.client.get(
            f'/messaging/open/?guest={self.guest.pk}&template=rent_reminder',
            HTTP_USER_AGENT=LAPTOP)
        self.assertIn('web.whatsapp.com', response['Location'])

    def test_it_uses_the_whatsapp_number_when_there_is_one(self):
        self.guest.whatsapp_number = '0559998888'
        self.guest.save()
        response = self.client.get(
            f'/messaging/open/?guest={self.guest.pk}&template=rent_reminder',
            HTTP_USER_AGENT=PHONE)
        self.assertIn('971559998888', response['Location'])

    def test_a_guest_with_no_number_is_reported_not_opened(self):
        self.guest.mobile = ''
        self.guest.whatsapp_number = ''
        self.guest.save()
        response = self.client.get(
            f'/messaging/open/?guest={self.guest.pk}&template=rent_reminder', follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MessageLog.objects.get().status, MessageLog.STATUS_FAILED)

    def test_the_hand_off_is_audited(self):
        self.client.get(f'/messaging/open/?guest={self.guest.pk}&template=rent_reminder')
        self.assertTrue(AuditLog.objects.filter(summary__contains='in WhatsApp').exists())

    def test_staff_need_the_create_grant(self):
        self.client.logout()
        self.grant('messaging', can_view=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get(
            f'/messaging/open/?guest={self.guest.pk}&template=rent_reminder'
        ).status_code, 403)


class WaitingListTests(ZyDwellTestCase):
    """Automatic reminders pile up with the API off — this is where they go."""

    def setUp(self):
        super().setUp()
        MessageTemplate.objects.filter(tenant=self.tenant).update(is_approved=True)
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=40),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        self.invoice = billing.build_invoice(
            self.tenancy, period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29),
            due_date=timezone.localdate(), user=self.owner)
        self.entry = services.send(
            tenant=self.tenant, template_code='rent_reminder',
            to_number=self.guest.messaging_number(),
            context=services.base_context(self.tenant, guest=self.guest,
                                          tenancy=self.tenancy, invoice=self.invoice),
            guest=self.guest, tenancy=self.tenancy, invoice=self.invoice,
        )
        self.client.login(username='owner', password='pw-owner-123')

    def test_an_unsent_reminder_waits_rather_than_vanishing(self):
        self.assertEqual(self.entry.status, MessageLog.STATUS_QUEUED)
        response = self.client.get('/messaging/waiting/')
        self.assertContains(response, self.guest.full_name)
        self.assertEqual(response.context['total'], 1)

    def test_opening_one_clears_it_from_the_list(self):
        response = self.client.get(f'/messaging/{self.entry.pk}/open/',
                                   HTTP_USER_AGENT=PHONE)
        self.assertEqual(response.status_code, 302)
        self.assertIn('wa.me', response['Location'])

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, MessageLog.STATUS_OPENED)
        self.assertEqual(self.entry.channel, CHANNEL_CLICK)
        self.assertEqual(self.entry.sent_by, self.owner)
        self.assertEqual(self.client.get('/messaging/waiting/').context['total'], 0)

    def test_the_page_says_who_is_expected_to_send_them(self):
        html = self.client.get('/messaging/waiting/').content.decode()
        self.assertIn('Nothing leaves the system by itself', html)
        self.assertIn('admin or a staff member', html)
        self.assertIn('their name goes on each one', html)

    def test_the_nightly_job_fills_the_list(self):
        from io import StringIO

        from django.core.management import call_command

        call_command('run_daily_jobs', '--tenant', self.tenant.slug, stdout=StringIO())
        waiting = MessageLog.objects.filter(status=MessageLog.STATUS_QUEUED).count()
        self.assertGreater(waiting, 0)
        self.assertEqual(self.client.get('/messaging/waiting/').context['total'], waiting)


class NewTabTests(ZyDwellTestCase):
    """A phone hands off to the WhatsApp app and leaves the new tab on
    about:blank, so chat links open in place there."""

    def setUp(self):
        super().setUp()
        self.client.login(username='owner', password='pw-owner-123')
        tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=40),
            monthly_rent=Decimal('900'), billing_day=1,
        )
        # An overdue invoice, so the WhatsApp buttons actually render.
        billing.build_invoice(
            tenancy, period_start=timezone.localdate() - timedelta(days=20),
            period_end=timezone.localdate() + timedelta(days=9),
            due_date=timezone.localdate() - timedelta(days=20), user=self.owner)

    def chat_links(self, html):
        import re

        return re.findall(r'<a[^>]*(?:quick_send|open_log|open/|send_manual)[^>]*>', html) + \
            re.findall(r'<a[^>]*href="[^"]*(?:/messaging/open/|/messaging/\d+/open/)[^"]*"[^>]*>',
                       html)

    def test_a_phone_never_gets_target_blank(self):
        html = self.client.get(f'/guests/{self.guest.pk}/',
                               HTTP_USER_AGENT=PHONE).content.decode()
        self.assertIn('/messaging/send/', html)
        for anchor in self.chat_links(html):
            self.assertNotIn('target="_blank"', anchor,
                             'a phone would be left staring at about:blank')

    def test_a_desktop_does_get_a_new_tab(self):
        html = self.client.get('/billing/overdue/',
                               HTTP_USER_AGENT=LAPTOP).content.decode()
        self.assertIn('/messaging/open/', html)
        self.assertIn('target="_blank"', html)

    def test_the_overdue_list_follows_the_device(self):
        phone = self.client.get('/billing/overdue/',
                                HTTP_USER_AGENT=PHONE).content.decode()
        laptop = self.client.get('/billing/overdue/',
                                 HTTP_USER_AGENT=LAPTOP).content.decode()
        self.assertNotEqual('target="_blank"' in phone, 'target="_blank"' in laptop)
