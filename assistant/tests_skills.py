"""The assistant's jobs and questions, driven the way a person would type them."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from accounting.models import KIND_EXPENSE, LedgerEntry
from accounts.models import StaffAssignment
from billing.models import Invoice, Payment
from core.constants import MODULE_ACCOUNTING, MODULE_BILLING, MODULE_GUESTS, MODULE_PROPERTIES
from core.tests import ZyDwellTestCase
from guests import services
from guests.models import Guest, Tenancy
from properties.models import Building, Floor


class AssistantCase(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.today = timezone.localdate()
        # A real name: the fixture's "Test Guest" holds the command word "guest".
        self.ravi = Guest.objects.create(tenant=self.tenant, full_name='Ravi Kumar',
                                         mobile='0551234567')

    def say(self, text):
        response = self.client.post(reverse('assistant:message'), {'text': text})
        self.assertEqual(response.status_code, 200)
        return response.json()['replies']

    def text(self, replies):
        return ' '.join(r['text'] for r in replies)

    def rows(self, replies):
        return {label: value for label, value in replies[-1].get('rows', [])}

    def sends(self, replies):
        return [b.get('send') for b in replies[-1].get('buttons', []) if b.get('send')]

    def stay(self, guest=None, bed=None, days_ago=0, invoice=True):
        tenancy = services.check_in(
            tenant=self.tenant, user=self.owner, guest=guest or self.ravi, room=self.room,
            bed=bed or self.beds[0], check_in_date=self.today - timedelta(days=days_ago),
            monthly_rent=Decimal('800'), deposit_amount=Decimal('800'),
            deposit_received=Decimal('800'))
        if invoice:
            services.raise_first_invoice(tenancy=tenancy, user=self.owner)
        return tenancy


class CheckInTests(AssistantCase):
    def test_one_sentence_check_in(self):
        replies = self.say('put ravi in room 101 bed B, deposit 500 paid')
        rows = self.rows(replies)
        self.assertIn('101 · B', rows['Bed'])
        self.assertEqual(rows['Monthly rent'], 'AED 800.00')     # the bed's price
        self.assertEqual(rows['Deposit received now'], 'AED 500.00')
        self.assertFalse(Tenancy.objects.filter(guest=self.ravi).exists())

        replies = self.say('yes')
        tenancy = Tenancy.objects.get(guest=self.ravi)
        self.assertEqual(tenancy.bed, self.beds[1])
        self.assertEqual(tenancy.deposit_held(), Decimal('500'))
        self.assertTrue(Invoice.objects.filter(tenancy=tenancy).exists())
        self.beds[1].refresh_from_db()
        self.assertTrue(self.beds[1].is_occupied)
        self.assertIn('checked in', self.text(replies))
        self.assertIn(f'@start:payment:tenancy={tenancy.pk}', ' '.join(self.sends(replies)))

    def test_room_only_asks_for_a_free_bed(self):
        self.stay(guest=Guest.objects.create(tenant=self.tenant, full_name='Other One',
                                             mobile='0551110000'))       # bed A taken
        replies = self.say('check in ravi to room 101')
        self.assertIn('Which bed', self.text(replies))
        self.assertNotIn(f'@pick:bed:{self.beds[0].pk}', self.sends(replies))
        self.assertIn(f'@pick:bed:{self.beds[1].pk}', self.sends(replies))
        replies = self.say(f'@pick:bed:{self.beds[2].pk}')
        self.assertIn('101 · C', self.rows(replies)['Bed'])

    def test_a_taken_bed_is_refused(self):
        self.stay(guest=Guest.objects.create(tenant=self.tenant, full_name='Other One',
                                             mobile='0551110000'))
        replies = self.say('put ravi in 101-A')
        self.assertIn('taken by Other One', self.text(replies))

    def test_a_whole_flat_needs_no_bed(self):
        replies = self.say('check in ravi to room 102')
        self.assertEqual(self.rows(replies)['Monthly rent'], 'AED 5,000.00')
        self.say('yes')
        self.assertIsNone(Tenancy.objects.get(guest=self.ravi).bed)

    def test_an_unknown_guest_can_be_added_first(self):
        replies = self.say('check in zubair to room 101 bed c')
        self.assertIn('could not find a guest', self.text(replies))
        self.assertIn('@start:add_guest:full_name=Zubair', self.sends(replies))

    def test_a_change_at_the_summary(self):
        self.say('put ravi in room 101 bed b')
        replies = self.say('rent 900')
        self.assertEqual(self.rows(replies)['Monthly rent'], 'AED 900.00')


class PaymentTests(AssistantCase):
    def test_paid_cash_goes_to_the_open_invoice(self):
        tenancy = self.stay()
        invoice = Invoice.objects.get(tenancy=tenancy)
        replies = self.say('ravi paid 100 cash')
        rows = self.rows(replies)
        self.assertEqual(rows['Amount'], 'AED 100.00')
        self.assertEqual(rows['Paid by'], 'Cash')
        self.assertIn(invoice.number, rows['For'])
        self.say('yes')
        payment = Payment.objects.get(tenancy=tenancy)
        self.assertEqual(payment.amount, Decimal('100'))
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal('100'))

    def test_card_asks_for_its_reference(self):
        self.stay()
        replies = self.say('ravi paid 100 by card')
        self.assertIn('reference', self.text(replies))
        replies = self.say('88123')
        self.assertEqual(self.rows(replies)['Reference'], '88123')

    def test_no_amount_offers_what_is_owed(self):
        tenancy = self.stay()
        replies = self.say('payment from ravi')
        self.assertIn(f'@pick:amount:{tenancy.outstanding()}', self.sends(replies))

    def test_more_than_owed_asks_what_to_do(self):
        tenancy = self.stay()
        owed = tenancy.outstanding()
        replies = self.say(f'ravi paid {owed + 500} cash')
        self.assertIn('owes only', self.text(replies))
        replies = self.say('@pick:excess:advance')
        self.assertIn('Advance credit', self.rows(replies)['For'])

    def test_staff_payment_waits_for_approval(self):
        self.stay()
        self.grant(MODULE_GUESTS)
        self.grant(MODULE_BILLING, can_create=True)
        self.client.force_login(self.staff)
        replies = self.say('ravi paid 100 cash')
        self.assertIn('approve', self.text(replies))
        self.say('yes')
        self.assertTrue(Payment.objects.get(amount=Decimal('100')).awaits_approval)

    def test_without_billing_rights_it_is_refused(self):
        self.stay()
        self.grant(MODULE_GUESTS, can_create=True)
        self.client.force_login(self.staff)
        self.assertIn('permission', self.text(self.say('ravi paid 100 cash')))
        self.assertFalse(Payment.objects.exists())

    def test_staff_only_find_stays_in_their_buildings(self):
        self.stay()
        elsewhere = Building.objects.create(tenant=self.tenant, name='Far Away')
        Floor.objects.create(tenant=self.tenant, building=elsewhere, name='G')
        StaffAssignment.objects.create(user=self.staff, building=elsewhere)
        self.grant(MODULE_GUESTS)
        self.grant(MODULE_BILLING, can_create=True)
        self.client.force_login(self.staff)
        self.assertIn('could not find anyone staying',
                      self.text(self.say('ravi paid 100 cash')))


class LeavingTests(AssistantCase):
    def test_check_out_today(self):
        tenancy = self.stay(days_ago=10)
        replies = self.say('ravi left today')
        rows = self.rows(replies)
        self.assertEqual(rows['Deposit held'], 'AED 800.00')
        self.assertEqual(rows['Refund to guest'], 'AED 800.00')
        self.say('yes')
        tenancy.refresh_from_db()
        self.assertEqual(tenancy.status.code, 'vacated')
        self.beds[0].refresh_from_db()
        self.assertFalse(self.beds[0].is_occupied)
        self.assertEqual(tenancy.deposit_held(), Decimal('0'))

    def test_a_deduction_needs_a_reason_and_lowers_the_refund(self):
        self.stay(days_ago=10)
        replies = self.say('check out ravi deduct 200')
        self.assertIn('deduction for', self.text(replies))
        replies = self.say('broken lock')
        rows = self.rows(replies)
        self.assertEqual(rows['Refund to guest'], 'AED 600.00')
        self.assertIn('broken lock', rows['Deduct'])

    def test_a_future_date_is_notice(self):
        tenancy = self.stay(days_ago=10)
        leaving = self.today + timedelta(days=12)
        replies = self.say(f'ravi leaving on {leaving:%d/%m/%Y}')
        self.assertIn('notice', self.text(replies))
        self.say('yes')
        tenancy.refresh_from_db()
        self.assertEqual(tenancy.status.code, 'notice')
        self.assertEqual(tenancy.expected_check_out, leaving)

    def test_notice_asks_when(self):
        self.stay()
        self.assertIn('When is', self.text(self.say('ravi gave notice')))


class ExpenseTests(AssistantCase):
    def test_electricity_bill(self):
        replies = self.say('paid dewa bill 450')
        rows = self.rows(replies)
        self.assertEqual(rows['Amount'], 'AED 450.00')
        self.assertIn('Electricity', rows['Category'])
        self.assertEqual(rows['For'], 'Test Tower')        # the only building
        self.say('yes')
        entry = LedgerEntry.objects.get(kind=KIND_EXPENSE, amount=Decimal('450'))
        self.assertEqual(entry.building, self.building)
        self.assertEqual(entry.recorded_by, self.owner)

    def test_category_can_be_changed(self):
        self.say('spent 120')
        replies = self.say('category cleaning')
        self.assertIn('Cleaning', self.rows(replies)['Category'])

    def test_needs_accounting_rights(self):
        self.grant(MODULE_GUESTS)
        self.client.force_login(self.staff)
        self.assertIn('permission', self.text(self.say('paid dewa bill 450')))


class QuestionTests(AssistantCase):
    def test_where_is_a_guest(self):
        tenancy = self.stay()
        replies = self.say('where is ravi')
        rows = self.rows(replies)
        self.assertIn('Test Tower', rows['Staying at'])
        self.assertEqual(rows['Owes'], f'AED {tenancy.outstanding():,.2f}')
        self.assertIn(f'@start:payment:tenancy={tenancy.pk}', self.sends(replies))
        self.assertNotIn('assistant', self.client.session)      # a question leaves no state

    def test_just_a_name_shows_the_guest(self):
        self.stay()
        self.assertIn('Staying at', self.rows(self.say('Ravi Kumar')))

    def test_free_beds(self):
        self.stay()
        replies = self.say('free beds')
        self.assertIn('1 of 4 beds taken', self.text(replies))
        self.assertEqual(len(replies[-1]['rows']), 3)

    def test_occupancy_by_building(self):
        self.stay()
        replies = self.say('how many beds occupied')
        self.assertIn('Test Tower', self.rows(replies))

    def test_who_has_not_paid(self):
        self.stay(days_ago=40)
        replies = self.say('who has not paid')
        self.assertIn('Ravi Kumar', ' '.join(self.rows(replies)))
        self.assertIn('days late', ' '.join(self.rows(replies).values()))

    def test_nobody_owes(self):
        self.assertIn('nobody owes', self.text(self.say('overdue list')))

    def test_todays_collection(self):
        tenancy = self.stay()
        from billing.services import record_payment
        record_payment(tenant=self.tenant, user=self.owner, tenancy=tenancy,
                       amount=Decimal('120'), invoice=Invoice.objects.get(tenancy=tenancy))
        replies = self.say("today's collection")
        self.assertIn('AED 120.00', self.text(replies))

    def test_expenses_this_month(self):
        self.say('electricity 300')
        self.say('yes')
        self.assertIn('AED 300.00', self.text(self.say('expenses this month')))

    def test_questions_respect_modules(self):
        self.grant(MODULE_GUESTS)
        self.client.force_login(self.staff)
        self.assertIn('permission', self.text(self.say('who has not paid')))
        self.grant(MODULE_PROPERTIES)
        self.assertIn('beds taken', self.text(self.say('free beds')))
        self.assertIn('access', self.text(self.say('expenses this month')))
        self.grant(MODULE_ACCOUNTING)
        self.assertIn('Expenses', self.text(self.say('expenses this month')))


class EngineTests(AssistantCase):
    def test_a_clear_new_request_replaces_the_old_one(self):
        self.stay()
        self.say('ravi paid by card')                 # waits for the amount
        replies = self.say('how many free beds are there')
        self.assertIn('leaving that', self.text(replies))
        self.assertIn('beds taken', self.text(replies))

    def test_a_button_can_start_another_job(self):
        tenancy = self.stay()
        replies = self.say(f'@start:check_out:tenancy={tenancy.pk}')
        self.assertIn('Check-out date', self.rows(replies))

    def test_a_start_button_cannot_reach_a_stay_out_of_scope(self):
        tenancy = self.stay()
        elsewhere = Building.objects.create(tenant=self.tenant, name='Far Away')
        StaffAssignment.objects.create(user=self.staff, building=elsewhere)
        self.grant(MODULE_GUESTS, can_edit=True)
        self.client.force_login(self.staff)
        replies = self.say(f'@start:check_out:tenancy={tenancy.pk}')
        self.assertIn('Who is leaving', self.text(replies))

    def test_an_old_pick_with_nothing_open(self):
        self.assertIn('closed', self.text(self.say('@pick:bed:1')))

    def test_help_lists_only_what_the_user_may_do(self):
        self.grant(MODULE_GUESTS)
        self.client.force_login(self.staff)
        text = self.text(self.say('help'))
        self.assertIn('where is Ravi', text)
        self.assertNotIn('paid 800', text)

    def test_a_vanished_stay_restarts_cleanly(self):
        tenancy = self.stay()
        self.say('ravi paid 100 cash')
        services.check_out(tenancy=tenancy, user=self.owner)
        replies = self.say('yes')
        self.assertEqual(replies[-1]['text'] and 200, 200)
        self.assertFalse(Payment.objects.filter(amount=Decimal('100')).exists())


class RealDataFixesTests(AssistantCase):
    """Cases found by trying the assistant on real records."""

    def test_refund_is_not_read_as_a_reference(self):
        self.stay(days_ago=10)
        replies = self.say('ravi left today refund 300')
        self.assertEqual(self.rows(replies)['Refund to guest'], 'AED 300.00')

    def test_a_refund_above_the_deposit_is_flagged_before_saving(self):
        self.stay(days_ago=10)
        replies = self.say('ravi left today refund 2000')
        self.assertIn('more than the deposit held', self.text(replies))

    def test_an_unknown_building_is_said_not_ignored(self):
        replies = self.say('free beds in atlantis')
        self.assertIn('could not find a building called "atlantis"', self.text(replies))
        self.assertIn('All your buildings', self.text(replies))

    def test_a_month_is_not_a_building(self):
        replies = self.say('expenses in october')
        self.assertNotIn('could not find', self.text(replies))
        self.assertIn('in October', self.text(replies))
