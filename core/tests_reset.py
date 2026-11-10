"""reset_data must empty the database without tripping over protected relations."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command
from django.utils import timezone

from accounting.models import KIND_EXPENSE, Category, LedgerEntry
from accounts.models import User
from billing import services as billing
from core.models import AuditLog, Status, Tenant
from core.tests import ZyDwellTestCase
from guests import services as guest_services
from guests.models import Guest, Tenancy
from properties.models import Bed, Building


class ResetDataTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        # Make the fixture look like a used system: a stay, invoices, receipts,
        # a deposit, a transfer and an expense — every protected relation.
        self.tenancy = guest_services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=40),
            monthly_rent=Decimal('900'), deposit_amount=Decimal('900'),
            deposit_received=Decimal('900'), billing_day=1,
        )
        invoice = billing.build_invoice(
            self.tenancy, period_start=timezone.localdate() - timedelta(days=10),
            period_end=timezone.localdate() + timedelta(days=19), user=self.owner)
        billing.record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy,
                               amount=Decimal('400'), invoice=invoice)
        guest_services.transfer_bed(tenancy=self.tenancy, user=self.owner,
                                    new_bed=self.beds[1])
        LedgerEntry.objects.create(
            tenant=self.tenant, kind=KIND_EXPENSE,
            category=Category.by_code(self.tenant, KIND_EXPENSE, 'cleaning'),
            amount=Decimal('120'), building=self.building)
        self.superadmin = User.objects.create_user(
            username='platform', password='pw-platform-123', role=User.ROLE_SUPER_ADMIN)

    def run_reset(self, *args):
        out = StringIO()
        call_command('reset_data', '--yes', '--no-backup', *args, stdout=out)
        return out.getvalue()

    def test_it_refuses_without_an_explicit_yes(self):
        with self.assertRaises(CommandError):
            call_command('reset_data', '--no-backup', stdout=StringIO())
        self.assertTrue(Tenant.objects.exists())

    def test_it_clears_client_data_without_a_protected_error(self):
        self.run_reset()
        self.assertEqual(Tenant.objects.count(), 0)
        for model in (Guest, Tenancy, Bed, Building, LedgerEntry, Category, AuditLog):
            self.assertEqual(model.objects.count(), 0, model.__name__)

    def test_the_super_admin_survives_by_default(self):
        self.run_reset()
        self.assertEqual(list(User.objects.values_list('username', flat=True)), ['platform'])

    def test_the_platform_status_templates_survive_by_default(self):
        Status.objects.filter(tenant__isnull=True).delete()
        Status.objects.create(tenant=None, kind=Status.KIND_BED, code='available',
                              name='Available', counts_as_available=True, is_default=True)
        self.run_reset()
        self.assertEqual(Status.objects.filter(tenant__isnull=True).count(), 1)
        self.assertEqual(Status.objects.filter(tenant__isnull=False).count(), 0)

    def test_all_users_removes_the_super_admin_too(self):
        self.run_reset('--all-users')
        self.assertEqual(User.objects.count(), 0)

    def test_platform_statuses_flag_clears_the_templates(self):
        self.run_reset('--platform-statuses')
        self.assertEqual(Status.objects.count(), 0)

    def test_it_reports_what_it_deleted(self):
        output = self.run_reset()
        self.assertIn('Database cleared', output)
        self.assertIn('billing.Payment', output)
        self.assertIn('platform', output)

    def test_running_it_twice_is_harmless(self):
        self.run_reset()
        self.assertIn('already empty', self.run_reset())

    def test_a_fresh_client_can_be_created_afterwards(self):
        """The point of keeping the templates: the next client is immediately usable."""
        from core.seed import seed_tenant

        self.run_reset()
        fresh = Tenant.objects.create(name='First Real Client', slug='first-real')
        seed_tenant(fresh)
        self.assertEqual(Status.objects.filter(tenant=fresh, kind=Status.KIND_BED).count(), 5)
        self.assertTrue(Category.objects.filter(tenant=fresh, code='monthly_rent').exists())
