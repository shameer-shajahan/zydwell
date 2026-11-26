"""The assistant: its model, its detail rules and the add-a-guest conversation."""
from django.test import SimpleTestCase
from django.urls import reverse

from core.constants import MODULE_GUESTS
from core.models import AuditLog
from core.tests import ZyDwellTestCase
from guests.models import Guest

from .brain import classifier, entities


class ClassifierTests(SimpleTestCase):
    def test_everyday_ways_of_asking_to_add_a_guest(self):
        for text in ['add a gust', 'add new gest', 'new tenant came today', 'pls register someone',
                     'a new boy joined today his name is ravi', 'i want add new guest',
                     'add guest Ravi Kumar +971 50 123 4567 indian male works at Emaar']:
            with self.subTest(text=text):
                self.assertEqual(classifier.predict(text)[0], 'add_guest')

    def test_answers_and_commands(self):
        for text, intent in [('ok', 'yes'), ('yes add', 'yes'), ('yess', 'yes'), ('no', 'no'),
                             ('cancel', 'cancel'), ('cancle', 'cancel'), ('stop it', 'cancel'),
                             ('help', 'help'), ('hi', 'greet'), ('thank you', 'thanks')]:
            with self.subTest(text=text):
                self.assertEqual(classifier.predict(text)[0], intent)

    def test_other_guest_work_is_not_adding_a_guest(self):
        for text in ['search guest ravi', 'delete the guest', 'show me guest list', 'add room']:
            with self.subTest(text=text):
                self.assertNotEqual(classifier.predict(text)[0], 'add_guest')

    def test_a_name_or_nonsense_is_not_a_command(self):
        for text in ['Ravi Kumar', 'Noel', 'Sanjay', 'blah blah', 'qwerty']:
            with self.subTest(text=text):
                self.assertEqual(classifier.predict(text)[0], classifier.UNKNOWN)

    def test_among_limits_the_answer(self):
        self.assertEqual(classifier.predict('add anyway', among={'yes', 'no', 'cancel'})[0], 'yes')


class EntityTests(SimpleTestCase):
    def test_everything_in_one_sentence(self):
        found = entities.extract(
            'add guest Ravi Kumar +971 50 123 4567 indian male works at Emaar')
        self.assertEqual(found, {
            'full_name': 'Ravi Kumar', 'mobile': '+971501234567', 'nationality': 'Indian',
            'gender': 'male', 'employer': 'Emaar'})

    def test_typo_in_the_command_is_not_taken_as_the_name(self):
        self.assertEqual(entities.extract('add a gust named ravi 0501234567'),
                         {'full_name': 'Ravi', 'mobile': '0501234567'})
        self.assertEqual(entities.extract('pls add one guest'), {})

    def test_phone_formats(self):
        for text, mobile in [('050 123 4567', '0501234567'), ('+971-50-123-4567', '+971501234567'),
                             ('00971 50 123 4567', '+971501234567'), ('(050) 123 4567', '0501234567'),
                             ('+91 98765 43210', '+919876543210')]:
            with self.subTest(text=text):
                self.assertEqual(entities.extract(text)['mobile'], mobile)

    def test_a_rent_after_the_mobile_is_left_alone(self):
        self.assertEqual(entities.extract('ravi 050 123 4567 1500')['mobile'], '0501234567')

    def test_name_modes(self):
        self.assertEqual(entities.extract('his name is Ravi Kumar', name='whole'),
                         {'full_name': 'Ravi Kumar'})
        self.assertEqual(entities.extract('Will Smith', name='whole'), {'full_name': 'Will Smith'})
        self.assertEqual(entities.extract('add guest', name='whole'), {})
        self.assertEqual(entities.extract('yes', name='marker'), {})
        self.assertEqual(entities.extract('change name to Ravi K Menon', name='marker'),
                         {'full_name': 'Ravi K Menon'})

    def test_other_details(self):
        found = entities.extract('new filipina lady maria santos 0551112222 email maria@x.com')
        self.assertEqual(found['nationality'], 'Filipino')
        self.assertEqual(found['gender'], 'female')
        self.assertEqual(found['email'], 'maria@x.com')
        self.assertEqual(found['full_name'], 'Maria Santos')
        self.assertEqual(entities.extract('from sri lanka')['nationality'], 'Sri Lankan')

    def test_same_mobile_written_two_ways(self):
        self.assertTrue(entities.same_mobile('0501234567', '+971 50 123 4567'))
        self.assertFalse(entities.same_mobile('0501234567', '0501234568'))


class ConversationTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def say(self, text):
        response = self.client.post(reverse('assistant:message'), {'text': text})
        self.assertEqual(response.status_code, 200)
        return response.json()['replies']

    def text(self, replies):
        return ' '.join(r['text'] for r in replies)

    def test_the_page_opens_with_a_greeting(self):
        response = self.client.get(reverse('assistant:chat'))
        self.assertContains(response, 'data-chat')
        self.assertContains(response, 'chat-opening')

    def test_one_sentence_then_yes_adds_the_guest(self):
        replies = self.say('add a gust ravi kumar 0551112222 indian works at Emaar')
        summary = replies[-1]
        self.assertIn(['Name', 'Ravi Kumar'], summary['rows'])
        self.assertIn(['Mobile', '0551112222'], summary['rows'])
        self.assertFalse(Guest.objects.filter(full_name='Ravi Kumar').exists())

        replies = self.say('yes')
        guest = Guest.objects.get(full_name='Ravi Kumar')
        self.assertEqual(guest.tenant, self.tenant)
        self.assertEqual(guest.mobile, '0551112222')
        self.assertEqual(guest.nationality, 'Indian')
        self.assertEqual(guest.employer, 'Emaar')
        self.assertTrue(AuditLog.objects.filter(
            action=AuditLog.ACTION_CREATE, object_label='Ravi Kumar').exists())
        hrefs = [b.get('href') for b in replies[-1]['buttons']]
        sends = [b.get('send') for b in replies[-1]['buttons']]
        self.assertIn(reverse('guests:guest_detail', args=[guest.pk]), hrefs)
        self.assertIn(f'@start:check_in:guest={guest.pk}', sends)
        self.assertNotIn('assistant', self.client.session)

    def test_step_by_step(self):
        self.assertIn('name', self.text(self.say('new guest')))
        self.assertIn('mobile', self.text(self.say('Maria Santos')))
        self.assertIn(['Name', 'Maria Santos'], self.say('+971 55 999 8888')[-1]['rows'])
        self.say('ok')
        self.assertTrue(Guest.objects.filter(full_name='Maria Santos',
                                             mobile='+971559998888').exists())

    def test_a_reply_without_a_number_is_asked_again(self):
        self.say('add guest Ravi')
        self.assertIn('could not find a mobile', self.text(self.say('tomorrow')))
        self.assertEqual(self.client.session['assistant']['awaiting'], 'mobile')

    def test_a_change_before_saving(self):
        self.say('add guest Ravi 0551112222')
        replies = self.say('mobile is 0553334444')
        self.assertIn(['Mobile', '0553334444'], replies[-1]['rows'])
        self.say('yes')
        self.assertEqual(Guest.objects.get(full_name='Ravi').mobile, '0553334444')

    def test_cancel_part_way_saves_nothing(self):
        count = Guest.objects.count()
        self.say('add guest Ravi')
        self.assertIn('cancelled', self.text(self.say('cancel')))
        self.assertNotIn('assistant', self.client.session)
        self.assertEqual(Guest.objects.count(), count)

    def test_nonsense_at_the_summary_does_not_save(self):
        self.say('add guest Ravi 0551112222')
        self.say('qwerty')
        self.assertFalse(Guest.objects.filter(full_name='Ravi').exists())

    def test_an_existing_mobile_is_flagged(self):
        # self.guest has 0501234567; the same line written the international way.
        replies = self.say('add guest Ravi +971 50 123 4567')
        self.assertIn('Test Guest', self.text(replies))
        self.assertEqual(self.client.session['assistant']['awaiting'], 'duplicate')
        self.say('yes')                       # add anyway: on to the summary
        self.say('yes')
        self.assertTrue(Guest.objects.filter(full_name='Ravi').exists())

    def test_staff_without_create_permission_is_refused(self):
        self.grant(MODULE_GUESTS)             # view only
        self.client.force_login(self.staff)
        replies = self.say('add guest Ravi 0551112222')
        self.assertIn('permission', self.text(replies))
        self.say('yes')
        self.assertFalse(Guest.objects.filter(full_name='Ravi').exists())

    def test_staff_without_guests_cannot_use_it(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('assistant:message'), {'text': 'add guest'})
        self.assertEqual(response.status_code, 403)

    def test_get_is_not_allowed_on_the_message_endpoint(self):
        self.assertEqual(self.client.get(reverse('assistant:message')).status_code, 405)

    def test_the_page_picks_up_where_it_left_off(self):
        self.say('add guest Ravi')
        response = self.client.get(reverse('assistant:chat'))
        self.assertContains(response, 'Welcome back')


class LauncherTests(ZyDwellTestCase):
    """The round chat button on every page, and the pop-up's first messages."""

    def test_the_button_shows_on_other_pages(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('guests:guest_list'))
        self.assertContains(response, 'chat-fab')
        self.assertContains(response, 'id="chat-panel"')
        self.assertContains(response, reverse('assistant:opening'))

    def test_but_not_on_the_assistant_page_itself(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('assistant:chat'))
        self.assertNotContains(response, 'chat-fab')
        self.assertContains(response, 'class="chat" data-chat ', count=1)

    def test_nor_for_staff_without_the_guests_module(self):
        self.grant('billing')
        self.client.force_login(self.staff)
        response = self.client.get(reverse('billing:invoice_list'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'chat-fab')

    def test_staff_with_guests_get_it(self):
        self.grant(MODULE_GUESTS)
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('guests:guest_list')), 'chat-fab')

    def test_the_opening_messages(self):
        self.client.force_login(self.owner)
        replies = self.client.get(reverse('assistant:opening')).json()['replies']
        self.assertIn('who has not paid', replies[0]['text'])
        self.client.post(reverse('assistant:message'), {'text': 'add guest Ravi'})
        replies = self.client.get(reverse('assistant:opening')).json()['replies']
        self.assertIn('Welcome back', replies[0]['text'])
        self.assertIn('mobile', replies[-1]['text'])


class NotInTheSideBarTests(ZyDwellTestCase):
    def test_only_the_round_button_opens_it(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('guests:guest_list'))
        self.assertNotIn(reverse('assistant:chat'), [item['url'] for item in response.context['nav_items']])
        self.assertContains(response, 'chat-fab')
