"""Permanent guest deletion, the WhatsApp number, and the self-service link."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from accounting.models import LedgerEntry
from billing import services as billing
from billing.models import Invoice, Payment
from core.models import AuditLog
from core.tests import ZyDwellTestCase
from guests import services
from guests.models import Guest, GuestDocument, GuestInvite, Tenancy
from messaging.models import MessageLog, MessageTemplate


class WhatsAppNumberTests(ZyDwellTestCase):
    def test_it_falls_back_to_the_mobile(self):
        self.assertEqual(self.guest.messaging_number(), '0501234567')

    def test_a_separate_whatsapp_number_wins(self):
        self.guest.whatsapp_number = '0559998888'
        self.guest.save()
        self.assertEqual(self.guest.messaging_number(), '0559998888')

    def test_messages_go_to_the_whatsapp_number(self):
        from messaging import services as messaging

        self.guest.whatsapp_number = '0559998888'
        self.guest.save()
        tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'),
        )
        invoice = billing.build_invoice(
            tenancy, period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29), user=self.owner)
        payment = billing.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=tenancy,
            amount=Decimal('100'), invoice=invoice)
        entry = messaging.send_payment_confirmation(payment)
        self.assertEqual(entry.to_number, '971559998888')

    def test_a_guest_can_be_found_by_their_whatsapp_number(self):
        self.guest.whatsapp_number = '0559998888'
        self.guest.save()
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/guests/?q=0559998888')
        self.assertContains(response, self.guest.full_name)


class GuestDeletionTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=40),
            monthly_rent=Decimal('900'), deposit_amount=Decimal('900'),
            deposit_received=Decimal('900'), billing_day=1,
        )
        self.invoice = billing.build_invoice(
            self.tenancy, period_start=timezone.localdate() - timedelta(days=5),
            period_end=timezone.localdate() + timedelta(days=24), user=self.owner)
        self.payment = billing.record_payment(
            tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
            amount=Decimal('500'), invoice=self.invoice)
        GuestDocument.objects.create(
            tenant=self.tenant, guest=self.guest, doc_type=GuestDocument.TYPE_PASSPORT,
            number='P1', expiry_date=timezone.localdate() + timedelta(days=200))

    def test_the_footprint_counts_what_would_go(self):
        footprint = services.guest_footprint(self.guest)
        self.assertEqual(footprint['tenancies'], 1)
        self.assertEqual(footprint['invoices'], 1)
        self.assertEqual(footprint['payments'], 1)
        self.assertEqual(footprint['collected'], Decimal('500'))
        self.assertEqual(footprint['documents'], 1)
        self.assertIsNotNone(footprint['live_tenancy'])

    def test_purging_removes_every_trace_of_the_guest(self):
        services.purge_guest(guest=self.guest, user=self.owner)
        self.assertFalse(Guest.objects.filter(pk=self.guest.pk).exists())
        self.assertFalse(Tenancy.objects.exists())
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(GuestDocument.objects.exists())

    def test_it_releases_the_bed_they_were_still_in(self):
        """Otherwise the bed stays marked occupied with nobody in it."""
        services.purge_guest(guest=self.guest, user=self.owner)
        self.beds[0].refresh_from_db()
        self.assertEqual(self.beds[0].status.code, 'available')

    def test_the_income_they_paid_leaves_the_ledger_with_them(self):
        self.assertTrue(LedgerEntry.objects.filter(
            source_type=LedgerEntry.SOURCE_PAYMENT).exists())
        services.purge_guest(guest=self.guest, user=self.owner)
        self.assertFalse(LedgerEntry.objects.filter(
            source_type=LedgerEntry.SOURCE_PAYMENT).exists())

    def test_one_audit_entry_survives_with_the_totals(self):
        services.purge_guest(guest=self.guest, user=self.owner)
        entry = AuditLog.objects.filter(action=AuditLog.ACTION_DELETE,
                                        model_name='Guest').first()
        self.assertIsNotNone(entry)
        self.assertIn(self.guest.full_name, entry.object_label)
        self.assertEqual(entry.actor, self.owner)
        self.assertEqual(entry.changes['collected_before_deletion'], '500.00')
        self.assertEqual(entry.changes['removed']['payments'], 1)

    def test_receipt_numbers_are_not_handed_out_again(self):
        used = self.payment.receipt_number
        services.purge_guest(guest=self.guest, user=self.owner)
        other = Guest.objects.create(tenant=self.tenant, full_name='Next',
                                     mobile='0500000001')
        tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=other, room=self.room,
            bed=self.beds[1], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('800'))
        fresh = billing.record_payment(tenant=self.tenant, user=self.owner,
                                       tenancy=tenancy, amount=Decimal('10'))
        self.assertNotEqual(fresh.receipt_number, used)

    # --- the view ------------------------------------------------------
    def test_only_an_admin_may_open_the_delete_screen(self):
        self.grant('guests', can_view=True, can_edit=True, can_delete=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(
            self.client.get(f'/guests/{self.guest.pk}/delete/').status_code, 403)

    def test_the_screen_spells_out_what_will_be_destroyed(self):
        self.client.login(username='owner', password='pw-owner-123')
        html = self.client.get(f'/guests/{self.guest.pk}/delete/').content.decode()
        self.assertIn('This is permanent', html)
        self.assertIn('still checked in', html)

    def test_the_name_must_be_typed_to_confirm(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/guests/{self.guest.pk}/delete/',
                         {'confirm_name': 'wrong name'})
        self.assertTrue(Guest.objects.filter(pk=self.guest.pk).exists())

    def test_the_right_name_deletes_the_guest(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.post(f'/guests/{self.guest.pk}/delete/',
                                    {'confirm_name': self.guest.full_name}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Guest.objects.filter(pk=self.guest.pk).exists())


class ProfileLinkTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        MessageTemplate.objects.filter(tenant=self.tenant).update(is_approved=True)
        self.invite = GuestInvite.issue(guest=self.guest, user=self.owner, days=7)

    def test_the_token_is_long_and_unguessable(self):
        self.assertGreaterEqual(len(self.invite.token), 32)
        second = GuestInvite.issue(guest=self.guest, user=self.owner)
        self.assertNotEqual(self.invite.token, second.token)

    def test_issuing_a_new_link_kills_the_old_one(self):
        GuestInvite.issue(guest=self.guest, user=self.owner)
        self.invite.refresh_from_db()
        self.assertTrue(self.invite.is_revoked)
        self.assertEqual(self.client.get(f'/g/{self.invite.token}/').status_code, 410)

    def test_a_guest_can_open_the_link_without_signing_in(self):
        response = self.client.get(f'/g/{self.invite.token}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.guest.full_name)

    def test_opening_it_is_counted(self):
        self.client.get(f'/g/{self.invite.token}/')
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.opened_count, 1)

    def test_the_guest_can_fill_in_what_they_know(self):
        response = self.client.post(f'/g/{self.invite.token}/details/', {
            'full_name': 'Nadeem Akhtar', 'mobile': '0551112233',
            'whatsapp_number': '0559998888', 'email': 'nadeem@example.com',
            'nationality': 'Pakistani', 'date_of_birth': '1994-05-02', 'gender': 'male',
            'emergency_contact_name': 'Brother', 'emergency_contact_number': '0554445566',
            'employer': 'Transguard', 'employer_phone': '',
            'passport_number': 'P9988776', 'passport_expiry': '2030-01-31',
            'emirates_id_number': '', 'emirates_id_expiry': '',
            'visa_number': '', 'visa_expiry': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Thank you')

        self.guest.refresh_from_db()
        self.assertEqual(self.guest.full_name, 'Nadeem Akhtar')
        self.assertEqual(self.guest.whatsapp_number, '0559998888')
        self.assertEqual(self.guest.employer, 'Transguard')
        document = self.guest.documents.get(doc_type=GuestDocument.TYPE_PASSPORT)
        self.assertEqual(document.number, 'P9988776')

    REQUIRED = {'mobile': '0551112233', 'whatsapp_number': '0551112233',
                'gender': 'male', 'passport_number': 'P1234567'}

    def test_partial_answers_are_accepted(self):
        """Beyond the required few, a guest fills in what they know."""
        response = self.client.post(f'/g/{self.invite.token}/details/',
                                    {'full_name': 'Half Known', **self.REQUIRED})
        self.assertContains(response, 'Thank you')
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.full_name, 'Half Known')
        self.assertEqual(self.guest.documents.get(doc_type='passport').number, 'P1234567')

    def test_mobile_whatsapp_gender_and_passport_are_required(self):
        response = self.client.post(f'/g/{self.invite.token}/details/',
                                    {'full_name': 'Half Known'})
        self.assertNotContains(response, 'Thank you')
        form = response.context['form']
        self.assertEqual(set(self.REQUIRED) - set(form.errors), set())

    def test_an_expiry_without_a_number_is_queried(self):
        response = self.client.post(f'/g/{self.invite.token}/details/', {
            'full_name': self.guest.full_name, 'visa_expiry': '2030-01-01',
        })
        self.assertContains(response, 'Add the number as well')

    def test_the_submission_is_attributed_to_the_guest_in_the_audit_log(self):
        self.client.post(f'/g/{self.invite.token}/details/',
                         {'full_name': 'Signed Themselves', **self.REQUIRED})
        entry = AuditLog.objects.filter(model_name='Guest',
                                        action=AuditLog.ACTION_UPDATE).first()
        self.assertIsNone(entry.actor)
        self.assertIn('self-service link', entry.actor_label)

    def test_an_expired_link_is_refused(self):
        self.invite.expires_at = timezone.now() - timedelta(minutes=1)
        self.invite.save()
        response = self.client.get(f'/g/{self.invite.token}/')
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, 'expired', status_code=410)

    def test_a_made_up_token_gives_nothing_away(self):
        response = self.client.get('/g/not-a-real-token/')
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, self.guest.full_name, status_code=404)

    def test_the_page_carries_no_other_guest_data(self):
        other = Guest.objects.create(tenant=self.tenant, full_name='Someone Else',
                                     mobile='0509999999')
        html = self.client.get(f'/g/{self.invite.token}/').content.decode()
        self.assertNotIn(other.full_name, html)
        self.assertNotIn('Outstanding', html)

    def test_staff_can_issue_and_send_the_link(self):
        self.grant('guests', can_view=True, can_edit=True)
        self.client.login(username='staff', password='pw-staff-123')
        self.client.post(f'/guests/{self.guest.pk}/share-link/', {'days': '1'})
        invite = GuestInvite.objects.filter(guest=self.guest,
                                            revoked_at__isnull=True).first()
        self.assertIsNotNone(invite)

        self.client.get(f'/guests/links/{invite.pk}/send/')
        message = MessageLog.objects.filter(template_code='profile_link').first()
        self.assertIsNotNone(message)
        self.assertIn(invite.token, message.body)

    def test_a_revoked_link_stops_working(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post(f'/guests/links/{self.invite.pk}/revoke/')
        self.assertEqual(self.client.get(f'/g/{self.invite.token}/').status_code, 410)

    def test_the_share_panel_appears_on_the_guest_page(self):
        self.client.login(username='owner', password='pw-owner-123')
        html = self.client.get(f'/guests/{self.guest.pk}/').content.decode()
        self.assertIn(self.invite.token, html)
        self.assertIn('data-copy', html)
