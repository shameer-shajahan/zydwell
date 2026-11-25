"""Missed months are billed, billing runs once a day by itself, and pending rent is listed."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from billing import daily, services
from billing.models import Invoice
from core.models import Tenant
from core.tests import ZyDwellTestCase
from guests import services as stays


class CatchUpTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()

    def stay(self, days_ago, first_invoice=True):
        tenancy = stays.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=self.today - timedelta(days=days_ago),
            monthly_rent=Decimal('800'), billing_day=1)
        if first_invoice:
            stays.raise_first_invoice(tenancy=tenancy, user=self.owner)
        return tenancy

    def test_every_missed_month_is_billed_once(self):
        tenancy = self.stay(days_ago=75)
        created = services.generate_due_invoices(self.tenant, on_date=self.today)
        invoices = list(Invoice.objects.filter(tenancy=tenancy).order_by('period_start'))
        # Back to back, from the check-in up to the period that holds today.
        for before, after in zip(invoices, invoices[1:]):
            self.assertEqual(after.period_start, before.period_end + timedelta(days=1))
        self.assertLessEqual(invoices[-1].period_start, self.today)
        self.assertGreaterEqual(invoices[-1].period_end, self.today)
        self.assertGreaterEqual(len(created), 2)
        # The real due dates: the older ones show as late.
        self.assertLess(invoices[1].due_date, self.today)
        self.assertEqual(services.generate_due_invoices(self.tenant, on_date=self.today), [])

    def test_a_stay_never_invoiced_only_gets_the_current_period(self):
        tenancy = self.stay(days_ago=75, first_invoice=False)
        services.generate_due_invoices(self.tenant, on_date=self.today)
        invoices = Invoice.objects.filter(tenancy=tenancy)
        self.assertEqual(invoices.count(), 1)
        self.assertLessEqual(invoices.get().period_start, self.today)


class DailyRunTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        daily._done.clear()
        today = timezone.localdate()
        self.tenancy = stays.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=today - timedelta(days=45),
            monthly_rent=Decimal('800'), billing_day=1)
        stays.raise_first_invoice(tenancy=self.tenancy, user=self.owner)

    def test_it_runs_once_a_day(self):
        self.assertTrue(daily.bill_today(self.tenant))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.billing_run_on, timezone.localdate())
        daily._done.clear()                      # another server worker
        self.assertEqual(daily.bill_today(self.tenant), [])

    def test_the_first_visit_of_the_day_bills(self):
        self.client.force_login(self.owner)
        self.client.get(reverse('reports:dashboard'))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.billing_run_on, timezone.localdate())
        self.assertGreater(Invoice.objects.filter(tenancy=self.tenancy).count(), 1)

    def test_a_locked_account_is_not_billed(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(status=Tenant.STATUS_LOCKED)
        self.client.force_login(self.owner)
        self.client.get(reverse('reports:dashboard'))
        self.tenant.refresh_from_db()
        self.assertIsNone(self.tenant.billing_run_on)


class PendingOnOverduePageTests(ZyDwellTestCase):
    def test_pending_rent_is_listed_with_the_overdue(self):
        today = timezone.localdate()
        tenancy = stays.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=today, monthly_rent=Decimal('800'),
            billing_day=min(today.day, 28))
        invoice = stays.raise_first_invoice(tenancy=tenancy, user=self.owner)
        Invoice.objects.filter(pk=invoice.pk).update(due_date=today + timedelta(days=3))
        self.client.force_login(self.owner)
        response = self.client.get(reverse('billing:overdue'))
        self.assertContains(response, invoice.number)
        self.assertContains(response, 'Pending')


class LeavingDatePassedTests(ZyDwellTestCase):
    """A stay still open after its leaving date is not billed beyond it."""

    def test_no_negative_invoice_after_the_leaving_date(self):
        today = timezone.localdate()
        for first_invoice in (True, False):
            with self.subTest(first_invoice=first_invoice):
                Invoice.objects.all().delete()
                from guests.models import Tenancy
                Tenancy.objects.all().delete()
                tenancy = stays.check_in(
                    tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
                    bed=self.beds[0], check_in_date=today - timedelta(days=80),
                    expected_check_out=today - timedelta(days=50),
                    monthly_rent=Decimal('800'), billing_day=1)
                if first_invoice:
                    stays.raise_first_invoice(tenancy=tenancy, user=self.owner)
                services.generate_due_invoices(self.tenant, on_date=today)
                for invoice in Invoice.objects.filter(tenancy=tenancy):
                    self.assertGreaterEqual(invoice.period_end, invoice.period_start)
                    self.assertGreaterEqual(invoice.total, 0)
                    self.assertLessEqual(invoice.period_start, tenancy.expected_check_out)
