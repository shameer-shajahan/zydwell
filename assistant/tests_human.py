"""Messages typed the way people really type: typos, any word order, no verbs."""
from datetime import timedelta

from django.test import SimpleTestCase

from assistant.brain import classifier, parse, spelling
from assistant.models import LearnedPhrase
from guests.models import Guest, Tenancy
from properties.models import Apartment, Bed, Room

from .tests_skills import AssistantCase


class SpellingTests(SimpleTestCase):
    def test_command_words_are_written_out(self):
        self.assertEqual(
            spelling.normalise('check in recently aded gust akash on the appaertment 117'),
            'check in recently added guest akash on the apartment 117')
        self.assertEqual(spelling.normalise('chekin rcvd tdy'), 'check in received today')

    def test_names_are_never_changed(self):
        for name in ['Akash', 'Cassandra', 'Ravi', 'Yaseen', 'Shameer', 'Noel', 'Ajas', 'Rentaro']:
            with self.subTest(name=name):
                self.assertEqual(spelling.normalise(name), name)


class PlaceCodeTests(SimpleTestCase):
    def codes(self, text):
        return parse.place_codes(spelling.normalise(text))[0]

    def test_any_order(self):
        codes = self.codes('appaertment 117 a room a5 bed')
        self.assertIn(('117', 'apartment'), codes)
        self.assertIn(('a', 'room'), codes)
        self.assertIn(('a5', 'bed'), codes)

    def test_the_article_is_not_a_room(self):
        self.assertEqual(self.codes('give him a bed in room 101'), [('101', 'room')])

    def test_pairs_and_bare_codes(self):
        self.assertEqual(set(self.codes('101-B')), {('101', None), ('b', None)})
        self.assertEqual(self.codes('akash 103 a5'), [('103', None), ('a5', None)])

    def test_a_date_word_makes_a_day_not_money(self):
        from datetime import date
        today = date(2026, 11, 25)
        self.assertEqual(parse.find_date('date 1', today)[0], date(2026, 12, 1))
        self.assertEqual(parse.find_date('joined on 3', today)[0], date(2026, 12, 3))
        self.assertEqual(parse.day_of_month(20, today), date(2026, 11, 20))


class TypoTests(SimpleTestCase):
    def test_misspelt_requests(self):
        for text, intent in [('chekin akash 103 a6', 'check_in'), ('akash payd 600 cassh', 'payment'),
                             ('who hasnt payed', 'overdue'), ('fre beds', 'free_beds'),
                             ('expnses this mnth', 'money'), ('chek out ravi tdy', 'check_out'),
                             ('rcvd 500 frm sanjay', 'payment')]:
            with self.subTest(text=text):
                self.assertEqual(classifier.predict(text)[0], intent)

    def test_names_alone_are_still_not_commands(self):
        for name in ['Ravi Kumar', 'Akash', 'Noel', 'Priya Menon', 'Faisal Bin Saeed']:
            with self.subTest(name=name):
                self.assertEqual(classifier.predict(name)[0], classifier.UNKNOWN)


class FlatsLikeRealBuildingsCase(AssistantCase):
    """Apartment 117, room A, beds A1-A6 — the way the real buildings are named."""

    def setUp(self):
        super().setUp()
        self.apt = Apartment.objects.create(tenant=self.tenant, floor=self.floor, name='117')
        self.room_a = Room.objects.create(tenant=self.tenant, apartment=self.apt, number='A',
                                          capacity=6, rent_basis=Room.RENT_PER_BED)
        self.a_beds = {f'A{n}': Bed.objects.create(tenant=self.tenant, room=self.room_a,
                                                   label=f'A{n}', monthly_price=650)
                       for n in range(1, 7)}
        self.akash = Guest.objects.create(tenant=self.tenant, full_name='Akash',
                                          mobile='0559876543')


class HumanCheckInTests(FlatsLikeRealBuildingsCase):
    def test_the_sentence_from_the_owner(self):
        replies = self.say('check in recently aded gust akash on the appaertment 117 a room a5 bed')
        rows = self.rows(replies)
        self.assertEqual(rows['Guest'], 'Akash')
        self.assertIn('117 · A · A5', rows['Bed'])
        self.assertEqual(rows['Monthly rent'], 'AED 650.00')
        self.say('yes')
        self.assertEqual(Tenancy.objects.get(guest=self.akash).bed, self.a_beds['A5'])

    def test_recently_added_without_a_name(self):
        replies = self.say('put the recently added guest in 117 a3')
        self.assertEqual(self.rows(replies)['Guest'], 'Akash')

    def test_no_verb_at_all(self):
        replies = self.say('akash to 117 a2')
        self.assertIn('117 · A · A2', self.rows(replies)['Bed'])

    def test_a_flat_that_does_not_exist_is_said(self):
        Room.objects.create(tenant=self.tenant, apartment=Apartment.objects.create(
            tenant=self.tenant, floor=self.floor, name='118'), number='A', rent_basis=Room.RENT_PER_BED)
        replies = self.say('check in akash apartment 999 room a bed a4')
        self.assertIn('could not find 999', self.text(replies))

    def test_date_1_at_the_summary_is_the_date_not_the_rent(self):
        self.say('check in akash 117 a4')
        replies = self.say('date 1')
        rows = self.rows(replies)
        self.assertEqual(rows['Monthly rent'], 'AED 650.00')
        self.assertTrue(rows['Check-in date'].startswith('01 '))

    def test_a_bare_small_number_is_a_day(self):
        self.say('check in akash 117 a4')
        rows = self.rows(self.say('20'))
        self.assertEqual(rows['Monthly rent'], 'AED 650.00')
        self.assertTrue(rows['Check-in date'].startswith('20 '))

    def test_a_date_while_the_bed_is_asked_is_kept(self):
        self.say('check in akash')
        self.say('from 5th')
        replies = self.say('117 a6')
        self.assertTrue(self.rows(replies)['Check-in date'].startswith('05 '))


class TeachingTests(FlatsLikeRealBuildingsCase):
    def test_not_understood_offers_guesses_and_learns(self):
        self.stay(guest=self.akash, bed=self.a_beds['A1'])
        replies = self.say('akash gav notis')
        self.assertIn('Akash', self.text(replies))             # his card, and the question
        self.assertTrue(any(s.startswith('@teach:') for s in self.sends(replies)))
        replies = self.say('@teach:give_notice')
        self.assertIn('remember', self.text(replies))
        phrase = LearnedPhrase.objects.get(tenant=self.tenant)
        self.assertEqual(phrase.intent, 'give_notice')
        self.assertNotIn('akash', phrase.text)            # names are not learnt
        self.say('cancel')
        # Next time it is understood straight away.
        replies = self.say('akash gav notis')
        self.assertIn('When is', self.text(replies))

    def test_a_name_alone_just_shows_the_guest(self):
        replies = self.say('Akash')
        self.assertFalse(any(s.startswith('@teach:') for s in self.sends(replies)))

    def test_teaching_is_per_account(self):
        from core.models import Tenant
        other = Tenant.objects.create(name='Other', slug='other')
        LearnedPhrase.objects.create(tenant=other, text='zork blat', intent='overdue')
        self.assertNotEqual(classifier.predict('zork blat', model=classifier.model_for(
            self.tenant))[0], 'overdue')
        self.assertEqual(classifier.predict('zork blat', model=classifier.model_for(other))[0],
                         'overdue')

    def test_a_stale_teach_button(self):
        self.assertIn('closed', self.text(self.say('@teach:payment')))


class EditCheckInTests(FlatsLikeRealBuildingsCase):
    def setUp(self):
        super().setUp()
        self.tenancy = self.stay(guest=self.akash, bed=self.a_beds['A1'])     # in today

    def test_change_by_chat_moves_the_unpaid_first_invoice(self):
        from billing.models import Invoice
        new_day = self.today.replace(day=max(self.today.day - 3, 1)) if self.today.day > 1 \
            else self.today
        if new_day == self.today:
            self.skipTest('needs a day earlier in the same month')
        replies = self.say(f'change akash check in date to {new_day:%d/%m/%Y}')
        rows = self.rows(replies)
        self.assertIn('→', rows['Check-in date'])
        self.assertIn('will run from', rows['First invoice'])
        self.say('yes')
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.check_in_date, new_day)
        invoice = Invoice.objects.get(tenancy=self.tenancy)
        self.assertEqual(invoice.period_start, new_day)

    def test_a_paid_first_invoice_is_left_alone(self):
        from billing.models import Invoice
        from billing.services import record_payment
        invoice = Invoice.objects.get(tenancy=self.tenancy)
        record_payment(tenant=self.tenant, user=self.owner, tenancy=self.tenancy, amount=10,
                       invoice=invoice)
        if self.today.day == 1:
            self.skipTest('needs a day earlier in the same month')
        new_day = self.today.replace(day=self.today.day - 1)
        replies = self.say(f'akash actually came on {new_day:%d %b}')
        self.assertIn('already has money paid', self.rows(replies)['First invoice'])
        self.say('yes')
        invoice.refresh_from_db()
        self.assertEqual(invoice.period_start, self.today)

    def test_the_bed_must_have_been_free(self):
        from guests import services as stays
        earlier = Guest.objects.create(tenant=self.tenant, full_name='Before Him',
                                       mobile='0551112223')
        old = stays.check_in(tenant=self.tenant, user=self.owner, guest=earlier,
                             room=self.room_a, bed=self.a_beds['A2'],
                             check_in_date=self.today - timedelta(days=60), monthly_rent=650)
        stays.check_out(tenancy=old, user=self.owner,
                        check_out_date=self.today - timedelta(days=10))
        moved = self.stay(guest=self.ravi, bed=self.a_beds['A2'])
        problem = stays.check_in_date_problem(moved, self.today - timedelta(days=20))
        self.assertIn('Before Him', problem)
        self.assertIsNone(stays.check_in_date_problem(moved, self.today - timedelta(days=10)))

    def test_a_guest_not_staying_is_a_new_check_in(self):
        Guest.objects.create(tenant=self.tenant, full_name='Zara Khan', mobile='0551110001')
        replies = self.say('change zara check in date to 1st')
        self.assertIn('Which bed', self.text(replies))       # a new check-in asks for the bed
        self.assertEqual(self.client.session['assistant']['skill'], 'check_in')

    def test_check_in_with_a_date_for_someone_staying_is_a_correction(self):
        self.say('check in akash date 1')
        self.assertEqual(self.client.session['assistant']['skill'], 'edit_check_in')

    def test_the_web_form_changes_it_too(self):
        from django.urls import reverse
        new_day = self.today - timedelta(days=1)
        data = {'check_in_date': new_day.isoformat(), 'monthly_rent': '800',
                'rent_mode': self.tenancy.rent_mode, 'prorate_first_period': 'on',
                'billing_cycle': 'monthly', 'billing_day': '1', 'expected_check_out': '',
                'deposit_amount': '800', 'first_invoice_amount': '', 'agreement_note': ''}
        response = self.client.post(reverse('guests:tenancy_edit', args=[self.tenancy.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.check_in_date, new_day)

    def test_the_web_form_refuses_an_overlap(self):
        from django.urls import reverse
        from guests import services as stays
        earlier = Guest.objects.create(tenant=self.tenant, full_name='Before Him',
                                       mobile='0551112223')
        old = stays.check_in(tenant=self.tenant, user=self.owner, guest=earlier,
                             room=self.room_a, bed=self.a_beds['A3'],
                             check_in_date=self.today - timedelta(days=60), monthly_rent=650)
        stays.check_out(tenancy=old, user=self.owner,
                        check_out_date=self.today - timedelta(days=10))
        moved = self.stay(guest=self.ravi, bed=self.a_beds['A3'])
        data = {'check_in_date': (self.today - timedelta(days=30)).isoformat(),
                'monthly_rent': '800', 'rent_mode': moved.rent_mode, 'prorate_first_period': 'on',
                'billing_cycle': 'monthly', 'billing_day': '1', 'expected_check_out': '',
                'deposit_amount': '800', 'first_invoice_amount': '', 'agreement_note': ''}
        response = self.client.post(reverse('guests:tenancy_edit', args=[moved.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Before Him')
