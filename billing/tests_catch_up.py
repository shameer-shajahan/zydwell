"""Missed months are billed, the Run billing button stays inside the time limit, and
pending rent is listed."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from billing import services
from billing.models import Invoice
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

    def test_a_stay_never_invoiced_is_billed_from_check_in(self):
        tenancy = self.stay(days_ago=75, first_invoice=False)
        unbilled = services.unbilled_rent(tenancy, self.today)
        self.assertEqual(unbilled[0]['period_start'], tenancy.check_in_date)
        services.generate_due_invoices(self.tenant, on_date=self.today)
        invoices = list(Invoice.objects.filter(tenancy=tenancy).order_by('period_start'))
        self.assertEqual(invoices[0].period_start, tenancy.check_in_date)
        # What the page showed as not invoiced is exactly what was raised.
        self.assertEqual([(i.period_start, i.total) for i in invoices],
                         [(row['period_start'], row['rent']) for row in unbilled])
        self.assertEqual(services.unbilled_rent(tenancy, self.today), [])

    def test_a_part_period_is_charged_by_the_day(self):
        tenancy = self.stay(days_ago=0, first_invoice=False)       # bill day 1, mid-month
        row = services.unbilled_rent(tenancy, self.today)[0]
        days = (row['period_end'] - row['period_start']).days + 1
        self.assertEqual(row['rent'], tenancy.prorata(days, period_days=tenancy.period_length(
            tenancy.period_for(self.today)[0])))


class RunBillingButtonTests(ZyDwellTestCase):
    """The button stops before the web server's time limit and carries on next press."""

    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        self.tenancy = stays.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=today - timedelta(days=75),
            monthly_rent=Decimal('800'), billing_day=1)
        stays.raise_first_invoice(tenancy=self.tenancy, user=self.owner)

    def test_out_of_time_stops_and_keeps_what_was_raised(self):
        created, finished = services.generate_due_invoices_within(self.tenant, seconds=1e-9)
        self.assertEqual((created, finished), ([], False))
        created, finished = services.generate_due_invoices_within(self.tenant, seconds=20)
        self.assertTrue(finished)
        self.assertGreaterEqual(len(created), 2)

    def test_the_button(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('billing:run_billing'), follow=True)
        self.assertContains(response, 'invoice(s) raised')
        self.assertGreater(Invoice.objects.filter(tenancy=self.tenancy).count(), 1)

    def test_a_page_visit_does_not_bill(self):
        self.client.force_login(self.owner)
        before = Invoice.objects.count()
        self.client.get(reverse('reports:dashboard'))
        self.assertEqual(Invoice.objects.count(), before)


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


class NotInvoicedYetTests(ZyDwellTestCase):
    """Rent due but never invoiced shows on the overdue page and in the totals."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.tenancy = stays.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=self.today - timedelta(days=65),
            monthly_rent=Decimal('800'), billing_day=1)
        self.client.force_login(self.owner)

    def test_the_page_shows_every_pending_month_and_counts_it(self):
        owed = services.unbilled_rent(self.tenancy, self.today)
        self.assertGreaterEqual(len(owed), 3)
        response = self.client.get(reverse('billing:overdue'))
        self.assertContains(response, 'Dues by guest')
        self.assertContains(response, 'not invoiced')
        total = sum((row['rent'] for row in owed), Decimal('0'))
        self.assertEqual(response.context['total_due'], total)
        self.assertEqual(response.context['not_invoiced_total'], total)
        self.assertFalse(Invoice.objects.exists())            # nothing was saved

    def test_raise_invoices_for_one_guest(self):
        owed = services.unbilled_rent(self.tenancy, self.today)
        self.client.post(reverse('billing:raise_for_stay', args=[self.tenancy.pk]))
        self.assertEqual(Invoice.objects.filter(tenancy=self.tenancy).count(), len(owed))
        response = self.client.get(reverse('billing:overdue'))
        self.assertEqual(response.context['not_invoiced_total'], 0)
        self.assertEqual(response.context['total_due'], sum((r['rent'] for r in owed), Decimal('0')))
