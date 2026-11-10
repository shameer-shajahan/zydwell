"""Deposit receipts, and keeping deposits out of profit until they are earned."""
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlparse

from django.utils import timezone

from accounting.models import KIND_INCOME, LedgerEntry, totals
from billing import receipts, sharing
from core.models import AuditLog
from core.tests import ZyDwellTestCase
from guests import services
from guests.models import DepositTransaction

PHONE = 'iPhone; CPU iPhone OS 17_0 like Mac OS X'


class DepositReceiptTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=30),
            monthly_rent=Decimal('900'), deposit_amount=Decimal('900'),
            deposit_received=Decimal('900'),
        )
        self.entry = self.tenancy.deposit_transactions.get()
        self.client.login(username='owner', password='pw-owner-123')

    def test_every_deposit_movement_gets_its_own_receipt_number(self):
        self.assertTrue(self.entry.receipt_number.startswith('DEP'))
        second = services.record_deposit(tenancy=self.tenancy, user=self.owner,
                                         amount=Decimal('100'))
        self.assertNotEqual(self.entry.receipt_number, second.receipt_number)

    def test_the_printed_receipt_carries_what_it_must(self):
        text = '\n'.join(receipts.deposit_receipt_lines(self.entry))
        for needle in [self.building.name.upper(), self.entry.receipt_number,
                       self.guest.full_name, self.room.number, 'AMOUNT RECEIVED',
                       'Deposit now held']:
            self.assertIn(needle, text)

    def test_it_fits_both_paper_widths(self):
        from billing.models import PrinterConfig

        printer = PrinterConfig.objects.get(tenant=self.tenant)
        for width, columns in (('58', 32), ('80', 48)):
            printer.paper_width = width
            printer.save()
            lines = receipts.deposit_receipt_lines(self.entry, printer)
            self.assertTrue(all(len(line) <= columns for line in lines),
                            f'a line overflowed {columns} columns')

    def test_a_refund_receipt_reads_as_money_going_out(self):
        refund = DepositTransaction.objects.create(
            tenant=self.tenant, tenancy=self.tenancy,
            kind=DepositTransaction.KIND_REFUND, amount=Decimal('900'),
            reason='Checked out clean', recorded_by=self.owner)
        text = '\n'.join(receipts.deposit_receipt_lines(refund))
        self.assertIn('DEPOSIT REFUND', text)
        self.assertIn('AMOUNT PAID OUT', text)

    def test_the_staff_page_and_the_guest_page_both_work(self):
        page = self.client.get(f'/guests/deposits/{self.entry.pk}/receipt/')
        self.assertContains(page, self.entry.receipt_number)
        self.assertNotContains(page, 'receipt/pdf')

    def test_a_guest_can_read_their_deposit_receipt_from_a_link(self):
        url = sharing.deposit_url(self.entry)
        self.client.logout()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(self.entry.receipt_number, html)
        self.assertIn('Deposit held for you now', html)
        self.assertNotIn('<form', html.lower())

    def test_a_receipt_token_will_not_open_a_deposit(self):
        """Different salts, so one kind of link cannot be reused for the other."""
        entry, problem = sharing.deposit_from_token(
            sharing.signing.dumps({'d': self.entry.pk, 't': self.tenant.pk},
                                  salt=sharing.SALT))
        self.assertIsNone(entry)
        self.assertEqual(problem, 'invalid')

    def test_sending_it_opens_whatsapp_with_the_link(self):
        response = self.client.get(
            f'/guests/deposits/{self.entry.pk}/receipt/whatsapp/', HTTP_USER_AGENT=PHONE)
        self.assertEqual(response.status_code, 302)
        text = unquote(parse_qs(urlparse(response['Location']).query)['text'][0])
        self.assertIn(self.entry.receipt_number, text)
        self.assertIn('/d/', text)
        self.assertTrue(AuditLog.objects.filter(summary__contains='deposit receipt').exists())


class DepositAccountingTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate() - timedelta(days=60),
            monthly_rent=Decimal('900'), deposit_amount=Decimal('900'),
            deposit_received=Decimal('900'),
        )

    def test_holding_a_deposit_is_not_income(self):
        """It is the guest's money. Counting it as profit overstates earnings."""
        self.assertEqual(
            totals(LedgerEntry.objects.filter(tenant=self.tenant))['income'],
            Decimal('0'))
        self.assertEqual(services.deposits_held(self.tenant), Decimal('900'))

    def test_refunding_it_is_not_an_expense_either(self):
        services.check_out(tenancy=self.tenancy, user=self.owner,
                           refund_amount=Decimal('900'))
        figures = totals(LedgerEntry.objects.filter(tenant=self.tenant))
        self.assertEqual(figures['expense'], Decimal('0'))
        self.assertEqual(figures['income'], Decimal('0'))
        self.assertEqual(services.deposits_held(self.tenant), Decimal('0'))

    def test_a_deduction_is_income_because_you_keep_it(self):
        services.check_out(
            tenancy=self.tenancy, user=self.owner,
            deductions=[{'amount': Decimal('250'), 'reason': 'Broken door'}],
            refund_amount=Decimal('650'))
        entry = LedgerEntry.objects.get(source_type=LedgerEntry.SOURCE_DEPOSIT)
        self.assertEqual(entry.kind, KIND_INCOME)
        self.assertEqual(entry.amount, Decimal('250'))
        self.assertEqual(entry.category.code, 'security_deposit')
        self.assertEqual(entry.building, self.building)
        self.assertEqual(services.deposits_held(self.tenant), Decimal('0'))

    def test_the_totals_add_up(self):
        services.check_out(
            tenancy=self.tenancy, user=self.owner,
            deductions=[{'amount': Decimal('250'), 'reason': 'Broken door'}],
            refund_amount=Decimal('650'))
        figures = services.deposit_totals(self.tenant)
        self.assertEqual(figures['received'], Decimal('900'))
        self.assertEqual(figures['deducted'], Decimal('250'))
        self.assertEqual(figures['refunded'], Decimal('650'))
        self.assertEqual(figures['paid_out'], Decimal('900'))
        self.assertEqual(figures['net'], Decimal('0'))

    def test_the_range_filter_only_counts_that_range(self):
        old = timezone.localdate() - timedelta(days=200)
        DepositTransaction.objects.create(
            tenant=self.tenant, tenancy=self.tenancy,
            kind=DepositTransaction.KIND_HOLD, amount=Decimal('500'), happened_on=old)
        today = timezone.localdate()
        window = services.deposit_totals(self.tenant, today - timedelta(days=7), today)
        self.assertEqual(window['received'], Decimal('900'))
        self.assertEqual(services.deposits_held(self.tenant), Decimal('1400'))


class DepositReportingTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=self.guest, room=self.room,
            bed=self.beds[0], check_in_date=timezone.localdate(),
            monthly_rent=Decimal('900'), deposit_amount=Decimal('900'),
            deposit_received=Decimal('900'))
        self.client.login(username='owner', password='pw-owner-123')

    def test_the_dashboard_shows_what_is_held(self):
        response = self.client.get('/')
        self.assertEqual(response.context['deposits_held'], Decimal('900'))
        self.assertEqual(response.context['deposit_month']['received'], Decimal('900'))
        self.assertContains(response, 'Deposits held')

    def test_the_report_lists_the_movements(self):
        response = self.client.get('/reports/deposits/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['overall']['net'], Decimal('900'))
        self.assertContains(response, self.guest.full_name)

    def test_it_exports(self):
        excel = self.client.get('/export/deposits/')
        self.assertTrue(excel.content.startswith(b'PK'))
        pdf = self.client.get('/export/deposits/?format=pdf')
        self.assertTrue(pdf.content.startswith(b'%PDF'))

    def test_deposits_ride_along_in_the_full_export(self):
        response = self.client.get('/export/everything/')
        self.assertTrue(response.content.startswith(b'PK'))

    def test_it_appears_on_the_reports_index(self):
        self.assertContains(self.client.get('/reports/'), 'Deposits')
