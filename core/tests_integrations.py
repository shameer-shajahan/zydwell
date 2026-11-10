"""Gateway keys the platform owner holds for one client."""
from django.test import override_settings

from core.models import AuditLog, IntegrationSettings, Tenant, whatsapp_config
from core.seed import seed_tenant
from core.tests import ZyDwellTestCase


class IntegrationKeyTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        from accounts.models import User

        self.platform = User.objects.create_user(
            username='platform', password='pw-platform-123',
            role=User.ROLE_SUPER_ADMIN)
        self.url = f'/platform/clients/{self.tenant.pk}/keys/'
        self.client.login(username='platform', password='pw-platform-123')

    def test_only_the_platform_owner_can_open_it(self):
        self.client.logout()
        self.client.login(username='owner', password='pw-owner-123')
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_a_client_admin_is_offered_no_way_in(self):
        self.client.logout()
        self.client.login(username='owner', password='pw-owner-123')
        html = self.client.get('/settings/').content.decode()
        self.assertNotIn('/keys/', html)

    def test_saving_keys(self):
        self.client.post(self.url, {
            'whatsapp_enabled': 'on',
            'whatsapp_api_url': 'https://graph.facebook.com/v21.0',
            'whatsapp_phone_number_id': '9988776655',
            'whatsapp_access_token': 'EAAG-secret-token-7788',
            'email_port': '587', 'email_use_tls': 'on',
        })
        row = IntegrationSettings.objects.get(tenant=self.tenant)
        self.assertTrue(row.whatsapp_enabled)
        self.assertEqual(row.whatsapp_access_token, 'EAAG-secret-token-7788')
        self.assertTrue(row.whatsapp_ready())

    def test_a_stored_secret_is_never_sent_back_to_the_browser(self):
        IntegrationSettings.objects.update_or_create(
            tenant=self.tenant,
            defaults={'whatsapp_access_token': 'EAAG-secret-token-7788',
                      'email_password': 'hunter2'})
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('EAAG-secret-token-7788', html)
        self.assertNotIn('hunter2', html)
        self.assertIn('••••7788', html)      # enough to tell which key it is

    def test_leaving_a_secret_blank_keeps_the_stored_one(self):
        IntegrationSettings.objects.update_or_create(
            tenant=self.tenant,
            defaults={'whatsapp_phone_number_id': '1', 'whatsapp_access_token': 'keep-me'})
        self.client.post(self.url, {
            'whatsapp_phone_number_id': '2', 'whatsapp_access_token': '',
            'email_port': '587',
        })
        row = IntegrationSettings.objects.get(tenant=self.tenant)
        self.assertEqual(row.whatsapp_access_token, 'keep-me')
        self.assertEqual(row.whatsapp_phone_number_id, '2')

    def test_switching_sending_on_without_a_token_is_refused(self):
        self.client.post(self.url, {'whatsapp_enabled': 'on', 'email_port': '587'})
        row = IntegrationSettings.objects.filter(tenant=self.tenant).first()
        self.assertFalse(row.whatsapp_enabled)

    def test_the_audit_log_records_the_change_but_not_the_key(self):
        self.client.post(self.url, {
            'whatsapp_phone_number_id': '9988776655',
            'whatsapp_access_token': 'EAAG-secret-token-7788',
            'email_port': '587',
        })
        entry = AuditLog.objects.filter(model_name='IntegrationSettings').first()
        self.assertIsNotNone(entry)
        self.assertIn('new whatsapp_access_token', entry.summary)
        self.assertNotIn('EAAG-secret-token-7788', entry.summary)
        self.assertNotIn('EAAG-secret-token-7788', str(entry.changes))


class WhatsAppConfigTests(ZyDwellTestCase):
    @override_settings(WHATSAPP={'ENABLED': False, 'API_URL': 'https://env.test',
                                 'PHONE_NUMBER_ID': 'env-id', 'ACCESS_TOKEN': 'env-token'})
    def test_without_client_keys_the_environment_is_used(self):
        config = whatsapp_config(self.tenant)
        self.assertEqual(config['PHONE_NUMBER_ID'], 'env-id')
        self.assertFalse(config['ENABLED'])

    @override_settings(WHATSAPP={'ENABLED': True, 'API_URL': 'https://env.test',
                                 'PHONE_NUMBER_ID': 'env-id', 'ACCESS_TOKEN': 'env-token'})
    def test_a_client_is_only_switched_on_deliberately(self):
        """The server default must not quietly start sending for every client."""
        IntegrationSettings.objects.create(tenant=self.tenant)
        self.assertFalse(whatsapp_config(self.tenant)['ENABLED'])

    def test_client_keys_win_over_the_environment(self):
        IntegrationSettings.objects.create(
            tenant=self.tenant, whatsapp_enabled=True,
            whatsapp_api_url='https://client.test',
            whatsapp_phone_number_id='client-id', whatsapp_access_token='client-token')
        config = whatsapp_config(self.tenant)
        self.assertTrue(config['ENABLED'])
        self.assertEqual(config['API_URL'], 'https://client.test')
        self.assertEqual(config['PHONE_NUMBER_ID'], 'client-id')
        self.assertEqual(config['ACCESS_TOKEN'], 'client-token')

    def test_one_client_being_switched_on_does_not_switch_on_another(self):
        other = Tenant.objects.create(name='Second', slug='second-acc')
        seed_tenant(other, with_subscription=False)
        IntegrationSettings.objects.create(
            tenant=self.tenant, whatsapp_enabled=True,
            whatsapp_phone_number_id='a', whatsapp_access_token='b')
        self.assertTrue(whatsapp_config(self.tenant)['ENABLED'])
        self.assertFalse(whatsapp_config(other)['ENABLED'])

    def test_the_waiting_page_follows_the_client_setting(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.assertFalse(self.client.get('/messaging/waiting/').context['api_on'])
        IntegrationSettings.objects.create(
            tenant=self.tenant, whatsapp_enabled=True,
            whatsapp_phone_number_id='a', whatsapp_access_token='b')
        self.assertTrue(self.client.get('/messaging/waiting/').context['api_on'])
