"""Ledger tagging, category rules, recurring bills and net profit."""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from accounting.models import (
    KIND_EXPENSE, KIND_INCOME, Category, LedgerEntry, RecurringExpense, totals,
)
from core.tests import ZyDwellTestCase


class LedgerTests(ZyDwellTestCase):
    def test_an_entry_fills_in_the_floor_and_building_from_the_room(self):
        entry = LedgerEntry.objects.create(
            tenant=self.tenant, kind=KIND_EXPENSE,
            category=Category.by_code(self.tenant, KIND_EXPENSE, 'maintenance'),
            amount=Decimal('300'), room=self.room, description='Tap repair',
        )
        self.assertEqual(entry.floor, self.floor)
        self.assertEqual(entry.building, self.building)

    def test_net_profit_is_income_minus_expense(self):
        income = Category.by_code(self.tenant, KIND_INCOME, 'other_income')
        expense = Category.by_code(self.tenant, KIND_EXPENSE, 'cleaning')
        LedgerEntry.objects.create(tenant=self.tenant, kind=KIND_INCOME, category=income,
                                   amount=Decimal('1000'), building=self.building)
        LedgerEntry.objects.create(tenant=self.tenant, kind=KIND_EXPENSE, category=expense,
                                   amount=Decimal('400'), building=self.building)
        figures = totals(LedgerEntry.objects.filter(tenant=self.tenant))
        self.assertEqual(figures['income'], Decimal('1000'))
        self.assertEqual(figures['expense'], Decimal('400'))
        self.assertEqual(figures['net'], Decimal('600'))

    def test_the_form_refuses_a_category_from_the_other_ledger(self):
        from accounting.forms import LedgerEntryForm

        form = LedgerEntryForm(
            data={'kind': KIND_EXPENSE,
                  'category': Category.by_code(self.tenant, KIND_INCOME, 'other_income').pk,
                  'amount': '100', 'entry_date': timezone.localdate(),
                  'building': self.building.pk},
            tenant=self.tenant, user=self.owner,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('category', form.errors)

    def test_an_entry_must_be_tagged_somewhere(self):
        from accounting.forms import LedgerEntryForm

        form = LedgerEntryForm(
            data={'kind': KIND_EXPENSE,
                  'category': Category.by_code(self.tenant, KIND_EXPENSE, 'cleaning').pk,
                  'amount': '100', 'entry_date': timezone.localdate()},
            tenant=self.tenant, user=self.owner,
        )
        self.assertFalse(form.is_valid())

    def test_categories_are_data_so_an_admin_can_add_one(self):
        self.client.login(username='owner', password='pw-owner-123')
        self.client.post('/accounting/categories/', {
            'kind': KIND_EXPENSE, 'name': 'Pest control', 'code': 'pest_control',
            'description': '', 'sort_order': 20, 'is_active': 'on',
        })
        self.assertTrue(Category.objects.filter(
            tenant=self.tenant, code='pest_control').exists())

    def test_automatic_entries_cannot_be_hand_edited(self):
        entry = LedgerEntry.objects.create(
            tenant=self.tenant, kind=KIND_INCOME,
            category=Category.by_code(self.tenant, KIND_INCOME, 'monthly_rent'),
            amount=Decimal('900'), building=self.building,
            source_type=LedgerEntry.SOURCE_PAYMENT, source_id='999',
        )
        self.client.login(username='owner', password='pw-owner-123')
        response = self.client.get(f'/accounting/{entry.pk}/edit/')
        self.assertEqual(response.status_code, 302)


class RecurringExpenseTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.rule = RecurringExpense.objects.create(
            tenant=self.tenant, name='DEWA', amount=Decimal('2400'), day_of_month=5,
            category=Category.by_code(self.tenant, KIND_EXPENSE, 'electricity_water'),
            building=self.building,
        )

    def test_it_raises_one_entry_per_month(self):
        first = self.rule.generate(date(2026, 3, 6))
        self.assertIsNotNone(first)
        self.assertEqual(first.amount, Decimal('2400'))
        self.assertEqual(first.entry_date, date(2026, 3, 5))
        self.assertIsNone(self.rule.generate(date(2026, 3, 20)))
        self.assertIsNotNone(self.rule.generate(date(2026, 4, 9)))

    def test_nothing_is_raised_before_the_chosen_day(self):
        self.assertIsNone(self.rule.generate(date(2026, 3, 2)))

    def test_an_inactive_rule_raises_nothing(self):
        self.rule.is_active = False
        self.rule.save()
        self.assertIsNone(self.rule.generate(date(2026, 3, 9)))


class DeleteExpenseTests(ZyDwellTestCase):
    def setUp(self):
        super().setUp()
        self.rule = RecurringExpense.objects.create(
            tenant=self.tenant, name='DEWA', amount=Decimal('2400'), day_of_month=5,
            category=Category.by_code(self.tenant, KIND_EXPENSE, 'electricity_water'),
            building=self.building,
        )
        self.client.login(username='owner', password='pw-owner-123')

    def test_a_raised_recurring_bill_can_be_deleted(self):
        entry = self.rule.generate(date(2026, 3, 6))
        self.client.post(f'/accounting/{entry.pk}/delete/')
        self.assertFalse(LedgerEntry.objects.filter(pk=entry.pk).exists())

    def test_a_recurring_rule_is_deleted_but_its_bills_stay(self):
        entry = self.rule.generate(date(2026, 3, 6))
        self.client.post(f'/accounting/recurring/{self.rule.pk}/delete/')
        self.assertFalse(RecurringExpense.objects.filter(pk=self.rule.pk).exists())
        self.assertTrue(LedgerEntry.objects.filter(pk=entry.pk).exists())

    def test_a_payment_line_is_not_deleted_from_the_ledger(self):
        entry = LedgerEntry.objects.create(
            tenant=self.tenant, kind=KIND_INCOME, amount=Decimal('100'),
            category=Category.objects.filter(tenant=self.tenant, kind=KIND_INCOME).first(),
            source_type=LedgerEntry.SOURCE_PAYMENT, source_id='1')
        self.client.post(f'/accounting/{entry.pk}/delete/')
        self.assertTrue(LedgerEntry.objects.filter(pk=entry.pk).exists())
