"""Invoices, payments, receipts, late-fee rules and POS printer setup."""
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounting.models import KIND_INCOME, Category, LedgerEntry
from core.models import Sequence, Status, TenantScopedModel
from guests.models import Tenancy

TWOPLACES = Decimal('0.01')


class LateFeeRule(TenantScopedModel):
    KIND_FIXED = 'fixed'
    KIND_PERCENT = 'percent'
    KIND_PER_DAY = 'per_day'

    name = models.CharField(max_length=80, default='Standard late fee')
    grace_days = models.PositiveIntegerField(default=5)
    kind = models.CharField(
        max_length=10,
        choices=[
            (KIND_FIXED, _('Fixed amount')),
            (KIND_PERCENT, _('Percent of rent')),
            (KIND_PER_DAY, _('Per day overdue')),
        ],
        default=KIND_FIXED,
    )
    value = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    max_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'),
                                     help_text=_('0 means no cap.'))
    is_active = models.BooleanField(default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.get_kind_display()})'

    def compute(self, invoice, on_date=None):
        on_date = on_date or timezone.localdate()
        if not self.is_active:
            return Decimal('0')
        overdue_days = (on_date - invoice.due_date).days - self.grace_days
        if overdue_days <= 0:
            return Decimal('0')
        if self.kind == self.KIND_FIXED:
            fee = self.value
        elif self.kind == self.KIND_PERCENT:
            fee = invoice.rent_amount * self.value / Decimal('100')
        else:
            fee = self.value * Decimal(overdue_days)
        if self.max_amount and fee > self.max_amount:
            fee = self.max_amount
        return fee.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


class PaymentMode(TenantScopedModel):
    """How money arrives. Data, not code — the admin adds their own.

    The flags matter more than the name: whether it lands in the cash drawer
    decides what a collector has to hand over at the end of the day.
    """

    name = models.CharField(max_length=40)
    code = models.SlugField()
    counts_as_cash = models.BooleanField(
        _('counts as cash in hand'), default=False,
        help_text=_('Included in the daily cash handover to the owner.'),
    )
    needs_reference = models.BooleanField(
        _('needs a reference'), default=False,
        help_text=_('Cheque number, UPI reference, card last four digits.'),
    )
    needs_due_date = models.BooleanField(
        _('needs a due date'), default=False,
        help_text=_('Post-dated cheques, so the office can be reminded.'),
    )
    settles_from_credit = models.BooleanField(
        _('draws on the guest’s advance'), default=False,
        help_text=_('No new money — it spends credit the guest already paid in.'),
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'code'], name='uniq_payment_mode'),
        ]

    def __str__(self):
        return self.name

    @classmethod
    def by_code(cls, tenant, code):
        return cls.objects.filter(tenant=tenant, code=code).first()

    @classmethod
    def default_for(cls, tenant):
        return cls.objects.filter(tenant=tenant, is_active=True).first()

    def in_use(self):
        return self.payments.exists() or self.deposit_transactions.exists()


class Invoice(TenantScopedModel):
    tenancy = models.ForeignKey(Tenancy, on_delete=models.PROTECT, related_name='invoices')
    number = models.CharField(max_length=40, unique=True)
    issue_date = models.DateField(default=timezone.localdate)
    period_start = models.DateField()
    period_end = models.DateField()
    due_date = models.DateField()

    rent_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    extras_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    discount_reason = models.CharField(max_length=200, blank=True)
    discount_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='discounts_approved',
    )
    late_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))

    status = models.ForeignKey(
        Status, on_delete=models.PROTECT, null=True, blank=True,
        limit_choices_to={'kind': Status.KIND_INVOICE}, related_name='invoices',
    )
    notes = models.CharField(max_length=200, blank=True)
    is_prorata = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='invoices_created',
    )

    class Meta:
        ordering = ['-issue_date', '-id']
        indexes = [
            models.Index(fields=['tenant', 'due_date']),
            models.Index(fields=['tenant', 'issue_date']),
            models.Index(fields=['tenant', 'status']),
        ]

    def __str__(self):
        return f'{self.number} — {self.tenancy.guest.full_name}'

    @property
    def guest(self):
        return self.tenancy.guest

    @property
    def balance(self):
        return (self.total - self.paid_amount).quantize(TWOPLACES)

    @property
    def is_settled(self):
        return self.balance <= Decimal('0')

    @property
    def days_overdue(self):
        if self.is_settled:
            return 0
        return max((timezone.localdate() - self.due_date).days, 0)

    def aging_bucket(self):
        days = self.days_overdue
        if days <= 0:
            return 'current'
        if days <= 30:
            return '0-30'
        if days <= 60:
            return '31-60'
        return '60+'

    def period_label(self):
        return f'{self.period_start:%d %b %Y} – {self.period_end:%d %b %Y}'

    def save(self, *args, **kwargs):
        if not self.number and self.tenant_id:
            self.number = Sequence.next_value(
                self.tenant, 'invoice', settings.ZYDWELL['INVOICE_PREFIX']
            )
        super().save(*args, **kwargs)

    def recalculate(self, save=True):
        self.extras_total = sum((l.amount for l in self.lines.all()), Decimal('0'))
        self.paid_amount = sum(
            (p.amount for p in self.payments.filter(is_reversed=False)), Decimal('0')
        )
        self.total = (
            self.rent_amount + self.extras_total + self.late_fee - self.discount_amount
        ).quantize(TWOPLACES)
        self.status = self._derive_status()
        if save:
            self.save(update_fields=[
                'extras_total', 'paid_amount', 'total', 'late_fee', 'discount_amount',
                'status', 'updated_at',
            ])
        return self

    def _derive_status(self):
        code = 'pending'
        if self.paid_amount >= self.total and self.total > 0:
            code = 'paid'
        elif self.paid_amount > 0:
            code = 'partly_paid'
        elif timezone.localdate() > self.due_date:
            code = 'overdue'
        if code in ('pending', 'partly_paid') and timezone.localdate() > self.due_date:
            code = 'overdue' if code == 'pending' else code
        return Status.by_code(self.tenant, Status.KIND_INVOICE, code) or self.status

    def apply_late_fee(self, rule=None):
        rule = rule or LateFeeRule.objects.filter(tenant=self.tenant, is_active=True).first()
        if rule is None or self.is_settled:
            return Decimal('0')
        fee = rule.compute(self)
        if fee != self.late_fee:
            self.late_fee = fee
            self.recalculate()
        return self.late_fee


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='lines')
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name='invoice_lines',
        limit_choices_to={'kind': KIND_INCOME},
    )
    description = models.CharField(max_length=160)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.description} {self.amount}'


class Payment(TenantScopedModel):
    APPROVAL_NOT_REQUIRED = 'not_required'
    APPROVAL_PENDING = 'pending'
    APPROVAL_APPROVED = 'approved'
    APPROVAL_REJECTED = 'rejected'
    APPROVAL_CHOICES = [
        (APPROVAL_NOT_REQUIRED, _('No approval needed')),
        (APPROVAL_PENDING, _('Waiting for the owner')),
        (APPROVAL_APPROVED, _('Approved')),
        (APPROVAL_REJECTED, _('Rejected')),
    ]

    receipt_number = models.CharField(max_length=40, unique=True)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, null=True, blank=True, related_name='payments',
        help_text=_('Empty for an advance held as credit.'),
    )
    tenancy = models.ForeignKey(Tenancy, on_delete=models.PROTECT, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    mode = models.ForeignKey(
        PaymentMode, on_delete=models.PROTECT, related_name='payments',
        null=True, blank=True,
    )
    reference = models.CharField(max_length=80, blank=True)
    cheque_due_date = models.DateField(null=True, blank=True)
    paid_on = models.DateField(default=timezone.localdate, db_index=True)
    note = models.CharField(max_length=200, blank=True)
    from_credit = models.BooleanField(
        default=False, help_text=_('Settled out of the guest’s advance credit.')
    )

    collected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payments_collected',
    )
    collected_by_name = models.CharField(max_length=150, blank=True)

    is_reversed = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payments_reversed',
    )
    reversal_reason = models.CharField(max_length=200, blank=True)

    handover = models.ForeignKey(
        'billing.CashHandover', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payments',
    )

    approval = models.CharField(
        _('approval'), max_length=14, choices=APPROVAL_CHOICES,
        default=APPROVAL_NOT_REQUIRED,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payments_approved',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-paid_on', '-id']
        indexes = [
            models.Index(fields=['tenant', 'paid_on']),
            models.Index(fields=['tenant', 'collected_by', 'paid_on']),
            models.Index(fields=['tenant', 'approval']),
        ]

    def __str__(self):
        return f'{self.receipt_number} {self.amount} ({self.mode or "—"})'

    @property
    def awaits_approval(self):
        return self.approval == self.APPROVAL_PENDING

    @property
    def is_rejected(self):
        return self.approval == self.APPROVAL_REJECTED

    @property
    def guest(self):
        return self.tenancy.guest

    def clean(self):
        super().clean()
        if self.amount is None or self.amount <= 0:
            raise ValidationError({'amount': _('Enter an amount greater than zero.')})
        if self.invoice_id and self.invoice.tenancy_id != self.tenancy_id:
            raise ValidationError({'invoice': _('That invoice belongs to a different tenancy.')})
        if self.mode_id and self.mode.needs_reference and not self.reference:
            raise ValidationError({'reference': _('Record the %(mode)s reference.') % {
                'mode': self.mode.name.lower()}})
        if self.mode_id and self.mode.needs_due_date and not self.cheque_due_date:
            raise ValidationError({'cheque_due_date': _('Record when it falls due.')})

    def save(self, *args, **kwargs):
        new = self.pk is None
        if not self.receipt_number and self.tenant_id:
            self.receipt_number = Sequence.next_value(
                self.tenant, 'receipt', settings.ZYDWELL['RECEIPT_PREFIX']
            )
        if self.collected_by_id and not self.collected_by_name:
            self.collected_by_name = self.collected_by.display_name()
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.invoice_id:
                self.invoice.recalculate()
            self._sync_ledger(created=new)

    def _sync_ledger(self, created=False):
        """Keep the income ledger in step with this receipt."""
        if self.from_credit:
            return
        existing = LedgerEntry.objects.filter(
            tenant=self.tenant, source_type=LedgerEntry.SOURCE_PAYMENT, source_id=str(self.pk)
        ).first()
        if self.is_reversed:
            if existing:
                existing.delete()
            return
        category = (
            Category.by_code(self.tenant, KIND_INCOME, 'monthly_rent')
            or Category.objects.filter(tenant=self.tenant, kind=KIND_INCOME).first()
        )
        if category is None:
            return
        values = {
            'kind': KIND_INCOME,
            'category': category,
            'amount': self.amount,
            'entry_date': self.paid_on,
            'description': f'{self.receipt_number} — {self.tenancy.guest.full_name}',
            'building': self.tenancy.building,
            'floor': self.tenancy.floor,
            'room': self.tenancy.room,
            'reference': self.receipt_number,
            'recorded_by': self.collected_by,
        }
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
            existing.save()
        else:
            LedgerEntry.objects.create(
                tenant=self.tenant,
                source_type=LedgerEntry.SOURCE_PAYMENT,
                source_id=str(self.pk),
                **values,
            )

    def reverse(self, user, reason):
        """Only an admin may reverse; the receipt number is retired, never reused."""
        self.is_reversed = True
        self.reversed_at = timezone.now()
        self.reversed_by = user
        self.reversal_reason = reason
        self.save()
        return self


class CashHandover(TenantScopedModel):
    """A collector handing the day's cash to the admin."""

    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='handovers'
    )
    handover_date = models.DateField(default=timezone.localdate)
    expected_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    declared_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    note = models.CharField(max_length=200, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='handovers_confirmed',
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-handover_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['staff', 'handover_date'], name='uniq_handover_per_day'),
        ]

    def __str__(self):
        return f'{self.staff.display_name()} — {self.handover_date} ({self.declared_amount})'

    @property
    def variance(self):
        return self.declared_amount - self.expected_amount

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None


class PrinterConfig(TenantScopedModel):
    WIDTH_58 = '58'
    WIDTH_80 = '80'
    CONNECTION_USB = 'usb'
    CONNECTION_NETWORK = 'network'
    CONNECTION_BLUETOOTH = 'bluetooth'

    name = models.CharField(max_length=80)
    paper_width = models.CharField(
        max_length=4, choices=[(WIDTH_58, '58 mm'), (WIDTH_80, '80 mm')], default=WIDTH_80
    )
    connection = models.CharField(
        max_length=12,
        choices=[
            (CONNECTION_USB, _('USB')),
            (CONNECTION_NETWORK, _('Network (IP:port)')),
            (CONNECTION_BLUETOOTH, _('Bluetooth')),
        ],
        default=CONNECTION_NETWORK,
    )
    address = models.CharField(
        max_length=120, blank=True, help_text=_('192.168.1.50:9100, a USB device path, or a MAC address.')
    )
    copies = models.PositiveSmallIntegerField(default=1)
    header_text = models.CharField(max_length=200, blank=True)
    footer_text = models.CharField(max_length=200, blank=True)
    is_default = models.BooleanField(default=False)
    last_test_at = models.DateTimeField(null=True, blank=True)
    last_test_ok = models.BooleanField(null=True, blank=True)
    last_test_message = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-is_default', 'name']

    def __str__(self):
        return f'{self.name} ({self.paper_width} mm)'

    @property
    def columns(self):
        return 32 if self.paper_width == self.WIDTH_58 else 48

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            PrinterConfig.objects.filter(tenant=self.tenant).exclude(pk=self.pk).update(is_default=False)
