"""Uploaded files are served with DEBUG off, and only to the right people."""
import io
import shutil
import tempfile
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.test import override_settings
from PIL import Image

from accounts.models import User
from core.constants import MODULE_GUESTS
from core.models import StoredFile, Tenant
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


def photo(width=4000, height=3000):
    out = io.BytesIO()
    Image.new('RGB', (width, height), (200, 120, 40)).save(out, 'JPEG', quality=95)
    return out.getvalue()


@override_settings(MEDIA_ROOT=MEDIA, DEBUG=False)
class DatabaseStorageTests(ZyDwellTestCase):
    """Uploads are kept in the database, which outlives every deploy."""

    def test_an_upload_is_saved_in_the_database_not_on_disk(self):
        self.guest.photo.save('face.jpg', ContentFile(photo(800, 600)))
        row = StoredFile.objects.get(name=self.guest.photo.name)
        self.assertTrue(row.name.startswith('guests/'))
        self.assertEqual(row.content_type, 'image/jpeg')
        self.assertFalse((Path(MEDIA) / row.name).exists())

    def test_a_big_phone_photo_is_shrunk(self):
        big = photo()
        self.guest.photo.save('big.jpg', ContentFile(big))
        row = StoredFile.objects.get(name=self.guest.photo.name)
        self.assertLess(row.size, len(big))
        with Image.open(io.BytesIO(bytes(row.content))) as image:
            self.assertEqual(max(image.size), 2000)

    def test_a_pdf_is_kept_exactly(self):
        pdf = b'%PDF-1.4 scan'
        doc = GuestDocument.objects.create(tenant=self.tenant, guest=self.guest,
                                           doc_type=GuestDocument.TYPE_VISA, number='V')
        doc.scan.save('visa.pdf', ContentFile(pdf))
        self.assertEqual(bytes(StoredFile.objects.get(name=doc.scan.name).content), pdf)

    def test_the_saved_photo_is_served(self):
        self.guest.photo.save('face.jpg', ContentFile(photo(400, 300)))
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get(self.guest.photo.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertEqual(b''.join(response.streaming_content),
                         bytes(StoredFile.objects.get(name=self.guest.photo.name).content))

    def test_the_same_name_twice_gets_a_new_name(self):
        first = default_storage.save('guests/x.jpg', ContentFile(b'one'))
        second = default_storage.save('guests/x.jpg', ContentFile(b'two'))
        self.assertNotEqual(first, second)
        self.assertEqual(default_storage.open(first).read(), b'one')

    def test_deleting_removes_it(self):
        name = default_storage.save('guests/x.jpg', ContentFile(b'one'))
        default_storage.delete(name)
        self.assertFalse(default_storage.exists(name))

    def test_files_left_on_disk_are_copied_in_once(self):
        folder = Path(MEDIA) / 'buildings'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'old-logo.png').write_bytes(b'\x89PNGold')
        call_command('move_media_to_db', stdout=io.StringIO())
        call_command('move_media_to_db', stdout=io.StringIO())
        self.assertEqual(StoredFile.objects.filter(name='buildings/old-logo.png').count(), 1)
        self.assertTrue((folder / 'old-logo.png').exists())

    def test_dry_run_saves_nothing(self):
        folder = Path(MEDIA) / 'ledger'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'bill.pdf').write_bytes(b'%PDF')
        call_command('move_media_to_db', '--dry-run', stdout=io.StringIO())
        self.assertFalse(StoredFile.objects.filter(name='ledger/bill.pdf').exists())
