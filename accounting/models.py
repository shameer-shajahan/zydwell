"""User-created income and expense categories, and the single money ledger."""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import TenantScopedModel

KIND_INCOME = 'income'
KIND_EXPENSE = 'expense'
KIND_CHOICES = [(KIND_INCOME, _('Income')), (KIND_EXPENSE, _('Expense'))]


class Category(TenantScopedModel):
    """Categories are data, not code — admin and staff can add their own."""

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    name = models.CharField(max_length=80)
    code = models.SlugField()
    description = models.CharField(max_length=160, blank=True)
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(
        default=False, help_text=_('Seeded category the system posts to automatically.')
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['kind', 'sort_order', 'name']
        verbose_name_plural = _('categories')
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'kind', 'code'], name='uniq_category_per_tenant'),
        ]

    def __str__(self):
        return f'{self.get_kind_display()}: {self.name}'

    @classmethod
    def by_code(cls, tenant, kind, code):
        return cls.objects.filter(tenant=tenant, kind=kind, code=code).first()


class LedgerEntry(TenantScopedModel):
    """Every dirham in or out, tagged to a building, floor or room.

    Rent payments post here automatically; expenses and other income are
    entered by hand. Net profit is simply income minus expense over a filter.
    """

    SOURCE_MANUAL = 'manual'
    SOURCE_PAYMENT = 'payment'
    SOURCE_DEPOSIT = 'deposit'
    SOURCE_RECURRING = 'recurring'
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, _('Manual entry')),
        (SOURCE_PAYMENT, _('Rent payment')),
        (SOURCE_DEPOSIT, _('Deposit movement')),
        (SOURCE_RECURRING, _('Recurring expense')),
    ]

    # Payment and deposit lines belong to their source; delete that instead.
    DELETABLE_SOURCES = (SOURCE_MANUAL, SOURCE_RECURRING)

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='entries')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    entry_date = models.DateField(default=timezone.localdate, db_index=True)
    description = models.CharField(max_length=200, blank=True)

    building = models.ForeignKey(
        'properties.Building', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ledger_entries',
    )
    floor = models.ForeignKey(
        'properties.Floor', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ledger_entries',
    )
    apartment = models.ForeignKey(
        'properties.Apartment', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ledger_entries',
    )
    room = models.ForeignKey(
        'properties.Room', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ledger_entries',
    )

    payee = models.CharField(max_length=120, blank=True, help_text=_('Expenses only.'))
    reference = models.CharField(max_length=60, blank=True)
    attachment = models.FileField(
        upload_to='ledger/', blank=True, null=True, help_text=_('Bill or invoice photo.')
    )

    source_type = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    source_id = models.CharField(max_length=40, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ledger_entries',
    )

    class Meta:
        ordering = ['-entry_date', '-id']
        verbose_name_plural = _('ledger entries')
        indexes = [
            models.Index(fields=['tenant', 'kind', 'entry_date']),
            models.Index(fields=['tenant', 'entry_date']),
        ]

    def __str__(self):
        return f'{self.entry_date} {self.get_kind_display()} {self.amount} — {self.category.name}'

    @property
    def signed_amount(self):
        return self.amount if self.kind == KIND_INCOME else -self.amount

    def save(self, *args, **kwargs):
        if self.room_id and not self.apartment_id:
            self.apartment = self.room.apartment
        if self.apartment_id and not self.floor_id:
            self.floor = self.apartment.floor
        if self.floor_id and not self.building_id:
            self.building = self.floor.building
        super().save(*args, **kwargs)


class RecurringExpense(TenantScopedModel):
    """A monthly bill that the system raises for you (DEWA, salary, internet…)."""

    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='recurring')
    name = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    day_of_month = models.PositiveSmallIntegerField(default=1)
    building = models.ForeignKey(
        'properties.Building', on_delete=models.CASCADE, null=True, blank=True,
        related_name='recurring_expenses',
    )
    floor = models.ForeignKey(
        'properties.Floor', on_delete=models.CASCADE, null=True, blank=True,
        related_name='recurring_expenses',
    )
    payee = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)
    last_generated_for = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} — {self.amount}/month'

    def due_for(self, on_date=None):
        on_date = on_date or timezone.localdate()
        if not self.is_active or on_date.day < min(self.day_of_month, 28):
            return None
        period = on_date.replace(day=1)
        if self.last_generated_for and self.last_generated_for >= period:
            return None
        return period

    def generate(self, on_date=None):
        period = self.due_for(on_date)
        if period is None:
            return None
        entry = LedgerEntry.objects.create(
            tenant=self.tenant,
            kind=KIND_EXPENSE,
            category=self.category,
            amount=self.amount,
            entry_date=period.replace(day=min(self.day_of_month, 28)),
            description=self.name,
            building=self.building,
            floor=self.floor,
            payee=self.payee,
            source_type=LedgerEntry.SOURCE_RECURRING,
            source_id=str(self.pk),
        )
        self.last_generated_for = period
        self.save(update_fields=['last_generated_for', 'updated_at'])
        return entry


def totals(queryset):
    """``{income, expense, net}`` for a ledger queryset."""
    agg = queryset.values('kind').annotate(total=models.Sum('amount'))
    data = {row['kind']: row['total'] or Decimal('0') for row in agg}
    income = data.get(KIND_INCOME, Decimal('0'))
    expense = data.get(KIND_EXPENSE, Decimal('0'))
    return {'income': income, 'expense': expense, 'net': income - expense}
