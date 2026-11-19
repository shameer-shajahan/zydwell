"""Uploaded files are served with DEBUG off, and only to the right people."""
import shutil
import tempfile
from pathlib import Path

from django.test import override_settings

from accounts.models import User
from core.constants import MODULE_GUESTS
from core.models import Tenant
from core.seed import seed_tenant
from core.tests import ZyDwellTestCase
from guests.models import GuestDocument

MEDIA = tempfile.mkdtemp(prefix='zydwell-media-test-')


@override_settings(MEDIA_ROOT=MEDIA, DEBUG=False)
class MediaServingTests(ZyDwellTestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.put('documents/IMG_20261118_200912.jpg', b'\xff\xd8passport')
        self.put('guests/face.jpg', b'\xff\xd8face')
        self.put('buildings/logo.png', b'\x89PNGlogo')
        self.put('documents/page.html', b'<script>alert(1)</script>')
        self.doc = GuestDocument.objects.create(
            tenant=self.tenant, guest=self.guest, doc_type=GuestDocument.TYPE_PASSPORT, number='P1',
            scan='documents/IMG_20261118_200912.jpg')
        GuestDocument.objects.create(tenant=self.tenant, guest=self.guest, doc_type=GuestDocument.TYPE_OTHER,
                                     number='X', scan='documents/page.html')
        self.guest.photo = 'guests/face.jpg'
        self.guest.save()

    def put(self, name, data):
        path = Path(MEDIA) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def body(self, response):
        return b''.join(response.streaming_content)

    def test_the_owner_sees_a_document_scan(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/media/documents/IMG_20261118_200912.jpg')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.body(response), b'\xff\xd8passport')
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertIn('private', response['Cache-Control'])

    def test_a_guest_photo_is_served(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.assertEqual(self.client.get('/media/guests/face.jpg').status_code, 200)

    def test_signed_out_is_sent_to_sign_in(self):
        response = self.client.get('/media/documents/IMG_20261118_200912.jpg')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/', response['Location'])

    def test_logos_are_public(self):
        response = self.client.get('/media/buildings/logo.png')
        self.assertEqual(response.status_code, 200)
        self.assertIn('public', response['Cache-Control'])

    def test_another_account_cannot_open_the_scan(self):
        other = Tenant.objects.create(name='Other', slug='other')
        seed_tenant(other, with_subscription=False)
        User.objects.create_user(username='rival', password='pw-rival-123',
                                 role=User.ROLE_ADMIN, tenant=other)
        self.client.login(username='rival', password='pw-rival-123')
        response = self.client.get('/media/documents/IMG_20261118_200912.jpg')
        self.assertEqual(response.status_code, 404)

    def test_staff_need_the_guests_module(self):
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get('/media/guests/face.jpg').status_code, 404)
        self.grant(MODULE_GUESTS)
        self.assertEqual(self.client.get('/media/guests/face.jpg').status_code, 200)

    def test_a_file_no_record_points_at_is_not_served(self):
        self.put('documents/stray.jpg', b'\xff\xd8')
        self.client.login(username='owner', password='pw-owner-123')
        self.assertEqual(self.client.get('/media/documents/stray.jpg').status_code, 404)

    def test_a_missing_file_is_a_404(self):
        GuestDocument.objects.create(tenant=self.tenant, guest=self.guest, doc_type=GuestDocument.TYPE_VISA,
                                     number='V', scan='documents/gone.jpg')
        self.client.login(username='owner', password='pw-owner-123')
        self.assertEqual(self.client.get('/media/documents/gone.jpg').status_code, 404)

    def test_leaving_the_media_folder_is_refused(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/media/documents/../../zydwell/settings.py')
        self.assertEqual(response.status_code, 404)

    def test_an_uploaded_web_page_downloads_and_cannot_run(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get('/media/documents/page.html')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Disposition'].startswith('attachment'))
        self.assertEqual(response['Content-Security-Policy'], 'sandbox')
