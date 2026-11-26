"""The assistant talking like a person, and the rest of the everyday jobs."""
from datetime import timedelta
from decimal import Decimal

from accounting.models import KIND_INCOME, LedgerEntry
from billing.models import Payment
from core.constants import MODULE_GUESTS
from guests.models import DepositTransaction, Guest, GuestDocument, Tenancy

from .tests_human import FlatsLikeRealBuildingsCase


class MemoryTests(FlatsLikeRealBuildingsCase):
    """After talking about someone, "he", "him" and "his" mean them."""

    def setUp(self):
        super().setUp()
        self.tenancy = self.stay(guest=self.akash, bed=self.a_beds['A1'])

    def test_he_paid_after_asking_about_him(self):
        self.say('where is akash')
        rows = self.rows(self.say('he paid 100 cash'))
        self.assertEqual(rows['Guest'], 'Akash')
        self.assertEqual(rows['Amount'], 'AED 100.00')

    def test_his_balance(self):
        self.say('akash paid 50 cash')
        self.say('yes')
        self.assertIn('Akash owes', self.say('his balance')[-1]['text'])

    def test_no_name_offers_the_last_guest(self):
        self.say('where is akash')
        replies = self.say('payment received')
        self.assertIn(f'@pick:tenancy:{self.tenancy.pk}', self.sends(replies))


class SmallTalkTests(FlatsLikeRealBuildingsCase):
    def test_greeting_uses_the_name(self):
        self.owner.first_name = 'Shameer'
        self.owner.save()
        self.assertIn('Shameer', self.say('hi')[0]['text'])

    def test_who_are_you_and_how_are_you(self):
        self.assertIn('ZyDwell assistant', self.text(self.say('who are you')))
        self.assertIn('doing well', self.text(self.say('how are you')))
        self.assertIn('Good night', self.text(self.say('good night')))


class TransferTests(FlatsLikeRealBuildingsCase):
    def setUp(self):
        super().setUp()
        self.tenancy = self.stay(guest=self.akash, bed=self.a_beds['A1'], days_ago=10)

    def test_move_to_another_bed(self):
        replies = self.say('move akash to 117 a a3')
        rows = self.rows(replies)
        self.assertIn('A1', rows['From'])
        self.assertIn('117 · A · A3', rows['To'])
        self.say('yes')
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.bed, self.a_beds['A3'])

    def test_a_check_in_for_someone_staying_is_a_move(self):
        self.say('put akash in 117 a a4')
        self.assertEqual(self.client.session['assistant']['skill'], 'transfer')

    def test_the_same_bed_is_refused(self):
        self.assertIn('already in', self.text(self.say('move akash to 117 a a1')))


class DepositTests(FlatsLikeRealBuildingsCase):
    def setUp(self):
        super().setUp()
        from guests import services as stays
        self.tenancy = stays.check_in(
            tenant=self.tenant, user=self.owner, guest=self.akash, room=self.room_a,
            bed=self.a_beds['A2'], check_in_date=self.today, monthly_rent=650,
            deposit_amount=Decimal('1000'), deposit_received=Decimal('400'))

    def test_take_the_rest_of_the_deposit(self):
        replies = self.say('akash paid deposit')
        self.assertIn('@pick:amount:600', ' '.join(self.sends(replies)))
        rows = self.rows(self.say('@pick:amount:600.00'))
        self.assertEqual(rows['Held after this'], 'AED 1,000.00')
        self.say('yes')
        self.assertEqual(self.tenancy.deposit_held(), Decimal('1000'))

    def test_a_payment_with_the_word_deposit_is_a_deposit(self):
        self.say('akash paid 300 security deposit cash')
        self.assertEqual(self.client.session['assistant']['skill'], 'deposit')
        self.say('yes')
        self.assertEqual(DepositTransaction.objects.filter(tenancy=self.tenancy).count(), 2)
        self.assertFalse(Payment.objects.filter(tenancy=self.tenancy).exists())


class EditGuestTests(FlatsLikeRealBuildingsCase):
    def test_change_the_mobile(self):
        rows = self.rows(self.say('change akash mobile to 0501112233'))
        self.assertEqual(rows['Mobile'], '0559876543 → 0501112233')
        self.say('yes')
        self.akash.refresh_from_db()
        self.assertEqual(self.akash.mobile, '0501112233')

    def test_a_new_name_is_not_taken_for_the_guest(self):
        rows = self.rows(self.say('change akash name to Akash Menon'))
        self.assertEqual(rows['Name'], 'Akash → Akash Menon')
        self.say('yes')
        self.akash.refresh_from_db()
        self.assertEqual(self.akash.full_name, 'Akash Menon')

    def test_nationality(self):
        self.say('akash nationality pakistani')
        self.say('yes')
        self.akash.refresh_from_db()
        self.assertEqual(self.akash.nationality, 'Pakistani')


class IncomeTests(FlatsLikeRealBuildingsCase):
    def test_laundry_income(self):
        rows = self.rows(self.say('received 100 for laundry'))
        self.assertIn('Laundry', rows['Category'])
        self.say('yes')
        self.assertTrue(LedgerEntry.objects.filter(kind=KIND_INCOME, amount=100).exists())


class ReminderTests(FlatsLikeRealBuildingsCase):
    def test_one_guest(self):
        self.stay(guest=self.akash, bed=self.a_beds['A1'], days_ago=40)
        replies = self.say('send reminder to akash')
        button = replies[-1]['buttons'][0]
        self.assertIn('/messaging/open/?guest=', button['href'])
        self.assertIn('template=rent_overdue', button['href'])
        self.assertTrue(button['new_tab'])

    def test_everyone_late(self):
        self.stay(guest=self.akash, bed=self.a_beds['A1'], days_ago=40)
        replies = self.say('remind everyone who has not paid')
        self.assertIn('1 guests are late', self.text(replies))

    def test_nobody_owes(self):
        self.assertIn('Nobody is late', self.text(self.say('remind everyone')))


class QuestionsMoreTests(FlatsLikeRealBuildingsCase):
    def test_documents_expiring(self):
        GuestDocument.objects.create(tenant=self.tenant, guest=self.akash, doc_type='visa',
                                     number='V1', expiry_date=self.today + timedelta(days=10))
        replies = self.say('whose visa is expiring')
        self.assertIn('Akash · Visa', self.rows(replies))

    def test_who_is_leaving_this_week(self):
        tenancy = self.stay(guest=self.akash, bed=self.a_beds['A1'], days_ago=20)
        Tenancy.objects.filter(pk=tenancy.pk).update(expected_check_out=self.today + timedelta(days=3))
        replies = self.say('who is leaving this week')
        self.assertTrue(any('Akash' in label for label in self.rows(replies)))

    def test_who_joined(self):
        self.stay(guest=self.akash, bed=self.a_beds['A1'])
        replies = self.say('who joined this month')
        self.assertTrue(any('Akash' in label for label in self.rows(replies)))

    def test_approvals_are_for_the_owner(self):
        self.assertIn('Nothing is waiting', self.text(self.say('pending approvals')))
        self.grant(MODULE_GUESTS)
        self.client.force_login(self.staff)
        self.assertIn('Only the owner', self.text(self.say('pending approvals')))


class SmallTalkMidwayTests(FlatsLikeRealBuildingsCase):
    def test_hi_while_a_question_is_open(self):
        self.say('payment received')                     # "Who paid?"
        replies = self.say('hi')
        self.assertIn('Good', replies[0]['text'])
        self.assertIn('Who paid', self.text(replies))
        self.assertEqual(self.client.session['assistant']['awaiting'], 'tenancy')

    def test_who_are_you_while_a_question_is_open(self):
        self.say('payment received')
        replies = self.say('who are you')
        self.assertIn('ZyDwell assistant', self.text(replies))
        self.assertIn('Who paid', self.text(replies))
