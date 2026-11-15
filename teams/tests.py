"""Team lets: whole flats let to companies, billed as one rent."""
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command


from billing import services as billing
from billing.models import Invoice
from core.tests import ZyDwellTestCase
from guests import services as guest_services
from guests.models import Guest, Tenancy
from properties.models import Apartment, Bed, Building, Floor, Room

from . import importer, services
from .models import TeamLet

TODAY = date(2026, 11, 15)
OCTOBER_CSV = Path(__file__).resolve().parent.parent / 'docs' / 'imports' / \
    'team_lets_october.csv'
TEAM_FLATS = ['102', '103', '104', '107', '108', '110', '112', '113', '114', '115', '116',
              '117']


class TeamTestCase(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch('django.utils.timezone.localdate', return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ahli = Building.objects.create(tenant=self.tenant, name='Al AHLI House A')
        floor = Floor.objects.create(tenant=self.tenant, building=self.ahli,
                                     name='First Floor', sort_order=1)
        self.flats = {}
        for number in range(101, 118):
            flat = Apartment.objects.create(tenant=self.tenant, floor=floor, name=str(number))
            room = Room.objects.create(tenant=self.tenant, apartment=flat, number='A',
                                       capacity=2, rent_basis=Room.RENT_PER_BED)
            for label in ('A1', 'A2'):
                Bed.objects.create(tenant=self.tenant, room=room, label=label)
            self.flats[str(number)] = flat
        self.efs = Guest.objects.create(tenant=self.tenant, is_company=True, full_name='EFS',
                                        mobile='971505146578')

    def let(self, flat='115', name='EFS TEAM BATCH 1', members=5, rent='3000', **extra):
        values = dict(tenant=self.tenant, user=self.owner, company=self.efs,
                      apartment=self.flats[flat], team_name=name, members=members,
                      per_head=Decimal('600'), monthly_rent=Decimal(rent), billing_day=1,
                      joining_date=date(2026, 2, 15))
        values.update(extra)
        return services.create_team_let(**values)

    def beds_of(self, flat):
        return Bed.objects.filter(room__apartment=self.flats[flat])


class TeamLetRuleTests(TeamTestCase):
    def test_a_team_let_is_a_tenancy_without_a_room(self):
        team = self.let()
        tenancy = Tenancy.objects.get(pk=team.pk)
        self.assertTrue(tenancy.is_team_let)
        self.assertIsNone(tenancy.room_id)
        self.assertEqual(tenancy.apartment, self.flats['115'])
        self.assertEqual(tenancy.building, self.ahli)
        self.assertEqual(tenancy.status.code, 'active')
        self.assertIn('EFS TEAM BATCH 1', tenancy.location_label())

    def test_several_teams_can_share_one_flat(self):
        self.let(name='EFS TEAM BATCH 1')
        self.let(name='EFS TEAM BATCH 2', members=3)
        self.let(name='EFS TEAM BATCH 3', members=28)
        self.assertEqual(TeamLet.objects.live().filter(apartment=self.flats['115']).count(), 3)

    def test_members_may_exceed_the_beds(self):
        team = self.let(flat='116', name='AUTO LIMOUSIN', members=79, rent='53325')
        self.assertEqual(team.members, 79)
        self.assertEqual(self.beds_of('116').count(), 2)

    def test_the_whole_flat_shows_occupied_and_counts_in_occupancy(self):
        self.let()
        self.assertTrue(all(bed.is_occupied for bed in self.beds_of('115')))
        self.assertEqual(self.flats['115'].occupancy()['occupied'], 2)

    def test_the_flat_frees_only_when_the_last_team_leaves(self):
        first = self.let(name='EFS TEAM BATCH 1')
        second = self.let(name='EFS TEAM BATCH 2')
        guest_services.check_out(tenancy=first, user=self.owner, check_out_date=TODAY)
        self.assertTrue(all(bed.is_occupied for bed in self.beds_of('115')))
        guest_services.check_out(tenancy=second, user=self.owner, check_out_date=TODAY)
        self.assertTrue(all(bed.is_available for bed in self.beds_of('115')))

    def test_a_team_flat_cannot_be_booked_bed_by_bed(self):
        self.let()
        self.client.login(username='owner', password='pw-owner-123')
        bed = self.beds_of('115').first()
        response = self.client.post('/guests/tenancies/check-in/', {
            'guest': self.guest.pk, 'room': bed.room_id, 'bed': bed.pk,
            'check_in_date': TODAY.isoformat(), 'monthly_rent': '800', 'billing_cycle':
            'monthly', 'billing_day': '1', 'rent_mode': 'advance', 'first_charge': 'full',
            'deposit_amount': '0', 'deposit_received': '0'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'is let to team')
        self.assertFalse(Tenancy.objects.filter(bed=bed).exists())

    def test_a_flat_with_guests_in_it_cannot_be_let_to_a_team(self):
        bed = self.beds_of('101').first()
        guest_services.check_in(tenant=self.tenant, user=self.owner, guest=self.guest,
                                room=bed.room, bed=bed, check_in_date=TODAY,
                                monthly_rent=Decimal('800'))
        with self.assertRaisesMessage(Exception, 'checked in one by one'):
            self.let(flat='101')

    def test_the_headcount_rate_is_only_a_hint(self):
        team = self.let(flat='110', name='FAYM', members=4, per_head=Decimal('450'),
                        rent='1225')
        self.assertTrue(team.rent_differs)
        self.assertEqual(team.headcount_rent, Decimal('1800.00'))
        self.assertEqual(team.monthly_rent, Decimal('1225'))


class TeamBillingTests(TeamTestCase):
    def test_the_nightly_run_bills_the_team_rent_once(self):
        team = self.let(rent='3000', billing_day=1)
        created = billing.generate_due_invoices(self.tenant, on_date=TODAY)
        invoice = Invoice.objects.get(tenancy=team)
        self.assertIn(invoice, created)
        self.assertEqual(invoice.period_start, date(2026, 11, 1))
        self.assertEqual(invoice.period_end, date(2026, 11, 30))
        self.assertEqual(invoice.total, Decimal('3000.00'))
        self.assertEqual(invoice.due_date, date(2026, 11, 1))
        billing.generate_due_invoices(self.tenant, on_date=TODAY)
        self.assertEqual(Invoice.objects.filter(tenancy=team).count(), 1)

    def test_the_billing_day_comes_from_the_team(self):
        team = self.let(flat='112', name='ADHIL TEAM', billing_day=3, rent='3800',
                        joining_date=date(2026, 7, 3))
        billing.generate_due_invoices(self.tenant, on_date=TODAY)
        invoice = Invoice.objects.get(tenancy=team)
        self.assertEqual(invoice.period_start, date(2026, 11, 3))
        self.assertEqual(invoice.period_end, date(2026, 12, 2))

    def test_raise_this_months_invoices_only_touches_teams(self):
        team = self.let()
        bed = self.beds_of('101').first()
        guest_services.check_in(tenant=self.tenant, user=self.owner, guest=self.guest,
                                room=bed.room, bed=bed, check_in_date=date(2026, 10, 1),
                                monthly_rent=Decimal('800'))
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post('/teams/raise-invoices/')
        self.assertEqual(list(Invoice.objects.values_list('tenancy', flat=True)), [team.pk])

    def test_payments_and_overdue_work_like_any_stay(self):
        team = self.let()
        billing.generate_due_invoices(self.tenant, on_date=date(2026, 11, 1))
        invoice = Invoice.objects.get(tenancy=team)
        billing.record_payment(tenant=self.tenant, user=self.owner, tenancy=team,
                               invoice=invoice, amount=Decimal('1000'))
        self.assertEqual(team.outstanding(), Decimal('2000.00'))
        self.client.login(username='owner', password='pw-owner-123')
        self.assertContains(self.client.get('/billing/overdue/'), 'EFS TEAM BATCH 1')
        page = self.client.get('/teams/')
        self.assertEqual(page.context['totals']['received'], Decimal('1000'))
        self.assertEqual(page.context['totals']['pending'], Decimal('2000.00'))

    def test_vacating_goes_through_check_out(self):
        team = self.let()
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.post(f'/guests/tenancies/{team.pk}/check-out/', {
            'check_out_date': TODAY.isoformat(), 'final_charge': 'keep',
            'deduction_amount': '0', 'refund_amount': '0', 'note': ''})
        self.assertRedirects(response, f'/guests/tenancies/{team.pk}/',
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.get(f'/guests/tenancies/{team.pk}/'),
                             f'/teams/{team.pk}/')
        team.refresh_from_db()
        self.assertEqual(team.status.code, 'vacated')
        self.assertTrue(all(bed.is_available for bed in self.beds_of('115')))


class TeamScreenTests(TeamTestCase):
    def test_owner_lets_a_flat_to_a_new_company(self):
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.post('/teams/new/', {
            'company': '', 'company_name': 'BASS TEAM', 'collector_mobile': '971506047143',
            'collector_name': 'Rafi', 'apartment': self.flats['114'].pk,
            'team_name': 'BASS TEAM', 'joining_date': '2026-01-04', 'billing_day': '1',
            'members': '48', 'per_head': '600', 'monthly_rent': '28800',
            'deposit_amount': '0', 'deposit_received': '0', 'agreement_note': ''})
        team = TeamLet.objects.get()
        self.assertRedirects(response, f'/teams/{team.pk}/', fetch_redirect_response=False)
        self.assertTrue(team.guest.is_company)
        self.assertEqual(team.guest.contact_person, 'Rafi')
        self.assertEqual(team.members, 48)

    def test_lists_keep_teams_and_guests_apart(self):
        self.let()
        self.client.login(username='owner', password='pw-owner-123')
        self.assertNotContains(self.client.get('/guests/tenancies/'), 'EFS TEAM BATCH 1')
        self.assertNotContains(self.client.get('/guests/'), '>EFS<')
        teams = self.client.get('/teams/')
        self.assertContains(teams, 'EFS TEAM BATCH 1')
        self.assertEqual(teams.context['totals']['members'], 5)

    def test_building_page_shows_the_team_on_the_flat(self):
        self.let(name='EFS TEAM BATCH 1', members=5)
        self.let(name='EFS TEAM BATCH 2', members=3)
        self.client.login(username='owner', password='pw-owner-123')
        page = self.client.get(f'/properties/{self.ahli.pk}/')
        self.assertContains(page, 'EFS TEAM BATCH 1, EFS TEAM BATCH 2 · 8 members')
        flats = {f['apartment'].name: f for block in page.context['floor_blocks']
                 for f in block['apartments']}
        self.assertEqual(len(flats['115']['teams']), 2)
        self.assertEqual(flats['101']['teams'], [])

    def test_the_dashboard_shows_team_rent(self):
        self.let(rent='3000')
        self.client.login(username='owner', password='pw-owner-123')
        page = self.client.get('/')
        self.assertEqual(page.context['teams']['rent'], Decimal('3000'))
        self.assertContains(page, 'team lets')

    def test_staff_need_guest_rights_to_see_teams(self):
        self.let()
        self.client.login(username='staff', password='pw-staff-123')
        self.assertEqual(self.client.get('/teams/').status_code, 403)
        self.grant('guests', can_view=True)
        self.assertEqual(self.client.get('/teams/').status_code, 200)
        self.assertEqual(self.client.get('/teams/import/').status_code, 403)


class TeamImportTests(TeamTestCase):
    def setUp(self):
        super().setUp()
        self.efs.delete()
        self.text = OCTOBER_CSV.read_text(encoding='utf-8')

    def test_the_october_sheet_imports_with_the_sheet_totals(self):
        plan = importer.read(self.text, self.tenant)
        self.assertTrue(plan.ok, [(r.line, r.errors) for r in plan.rows])
        self.assertEqual(plan.count_create, 15)
        importer.apply(plan, self.tenant, self.owner)

        teams = TeamLet.objects.live().filter(tenant=self.tenant)
        summary = services.live_summary(teams)
        self.assertEqual(summary, {'count': 15, 'members': 272, 'rent': Decimal('168325')})
        rows = list(services.with_money(teams, date(2026, 11, 1), date(2026, 11, 30)))
        self.assertEqual(services.totals(rows)['deposit'], Decimal('24600'))

        occupied = sorted({bed.room.apartment.name for bed in Bed.objects.filter(
            room__apartment__floor__building=self.ahli) if bed.is_occupied})
        self.assertEqual(occupied, TEAM_FLATS)
        # One company per collector mobile: EFS has five teams in three flats.
        efs = Guest.objects.get(is_company=True, mobile='971505146578')
        self.assertEqual(efs.full_name, 'EFS')
        self.assertEqual(efs.tenancies.count(), 5)
        self.assertEqual(Invoice.objects.count(), 0)

        adhil = TeamLet.objects.get(team_name='ADHIL TEAM')
        self.assertEqual(adhil.billing_day, 3)
        midhlaj = TeamLet.objects.get(team_name='MIDHLAJ')
        self.assertIsNone(midhlaj.joining_date)
        self.assertEqual(midhlaj.check_in_date, date(2026, 7, 1))
        aweer = TeamLet.objects.get(team_name='AWEER MARKET')
        self.assertIsNone(aweer.per_head)

    def test_importing_the_same_file_again_changes_nothing(self):
        importer.apply(importer.read(self.text, self.tenant), self.tenant, self.owner)
        again = importer.read(self.text, self.tenant)
        self.assertEqual(again.count_unchanged, 15)
        importer.apply(again, self.tenant, self.owner)
        self.assertEqual(TeamLet.objects.count(), 15)
        self.assertEqual(Guest.objects.filter(is_company=True).count(), 11)

    def test_a_changed_row_updates_the_team(self):
        importer.apply(importer.read(self.text, self.tenant), self.tenant, self.owner)
        changed = self.text.replace('BASS TEAM,971506047143,2026-01-04,2026-07-01,0,48,600,28800',
                                    'BASS TEAM,971506047143,2026-01-04,2026-07-01,0,50,600,30000')
        plan = importer.read(changed, self.tenant)
        self.assertEqual(plan.count_update, 1)
        importer.apply(plan, self.tenant, self.owner)
        bass = TeamLet.objects.get(team_name='BASS TEAM')
        self.assertEqual((bass.members, bass.monthly_rent), (50, Decimal('30000')))

    def test_an_unknown_apartment_is_an_error_and_nothing_is_saved(self):
        bad = self.text.replace(',,113,MIDHLAJ', ',,999,MIDHLAJ')
        plan = importer.read(bad, self.tenant)
        self.assertFalse(plan.ok)
        row = [r for r in plan.rows if r.action == importer.ERROR][0]
        self.assertIn('Apartment “999” not found in Al AHLI House A', row.errors[0])
        with self.assertRaises(ValueError):
            importer.apply(plan, self.tenant, self.owner)
        self.assertEqual(TeamLet.objects.count(), 0)

    def test_missing_columns_are_reported(self):
        plan = importer.read('building,apartment\nAl AHLI House A,115\n', self.tenant)
        self.assertFalse(plan.ok)
        self.assertIn('Missing columns', plan.file_errors[0])

    def test_the_web_import_previews_then_saves(self):
        self.client.login(username='owner', password='pw-owner-123')
        upload = SimpleUploadedFile('team_lets_october.csv', self.text.encode('utf-8'),
                                    content_type='text/csv')
        preview = self.client.post('/teams/import/', {'file': upload})
        self.assertContains(preview, 'Save these team lets')
        self.assertEqual(TeamLet.objects.count(), 0)
        saved = self.client.post('/teams/import/', {'csv_text': self.text, 'apply': '1'})
        self.assertRedirects(saved, '/teams/', fetch_redirect_response=False)
        self.assertEqual(TeamLet.objects.count(), 15)
        teams = self.client.get('/teams/')
        self.assertEqual(teams.context['totals']['members'], 272)
        self.assertEqual(teams.context['totals']['rent'], Decimal('168325'))
        self.assertEqual(teams.context['totals']['deposit'], Decimal('24600'))

    def test_the_command_dry_run_saves_nothing(self):
        out = StringIO()
        call_command('import_team_lets', str(OCTOBER_CSV), '--dry-run',
                     '--tenant', self.tenant.name, stdout=out)
        self.assertIn('15 create', out.getvalue())
        self.assertIn('272 members', out.getvalue())
        self.assertEqual(TeamLet.objects.count(), 0)
        call_command('import_team_lets', str(OCTOBER_CSV), '--tenant', self.tenant.name,
                     stdout=StringIO())
        self.assertEqual(TeamLet.objects.count(), 15)

    def test_the_command_refuses_a_file_with_errors(self):
        with mock.patch('builtins.open', mock.mock_open(
                read_data=self.text.replace(',,113,', ',,999,'))):
            with self.assertRaises(CommandError):
                call_command('import_team_lets', 'whatever.csv', '--tenant',
                             self.tenant.name, stdout=StringIO())
        self.assertEqual(TeamLet.objects.count(), 0)
