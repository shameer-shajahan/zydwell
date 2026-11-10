"""Guests, their documents, tenancies, transfers and deposits."""
import calendar
import secrets
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import Status, TenantScopedModel
from properties.models import Bed, Building, Floor, Room


class Guest(TenantScopedModel):
    full_name = models.CharField(max_length=150)
    mobile = models.CharField(max_length=32)
    whatsapp_number = models.CharField(
        _('WhatsApp number'), max_length=32, blank=True,
        help_text=_('Leave empty to send WhatsApp to the mobile number above.'),
    )
    email = models.EmailField(blank=True)
    photo = models.ImageField(upload_to='guests/', blank=True, null=True)
    nationality = models.CharField(max_length=80, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(
        max_length=12, blank=True,
        choices=[('male', _('Male')), ('female', _('Female')), ('other', _('Other'))],
    )
    emergency_contact_name = models.CharField(max_length=120, blank=True)
    emergency_contact_number = models.CharField(max_length=32, blank=True)
    employer = models.CharField(max_length=150, blank=True)
    employer_phone = models.CharField(max_length=32, blank=True)
    notes = models.TextField(blank=True)
    is_blacklisted = models.BooleanField(default=False)
    blacklist_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['full_name']
        indexes = [
            models.Index(fields=['tenant', 'mobile']),
            models.Index(fields=['tenant', 'full_name']),
        ]

    def __str__(self):
        return self.full_name

    def messaging_number(self):
        """Where WhatsApp is sent: the dedicated number, else the mobile."""
        return (self.whatsapp_number or self.mobile or '').strip()

    def current_tenancy(self):
        return (
            self.tenancies.filter(status__code__in=Tenancy.LIVE_STATUS_CODES)
            .select_related('bed', 'room', 'room__apartment', 'room__apartment__floor',
                            'room__apartment__floor__building')
            .first()
        )

    def outstanding_balance(self):
        from billing.models import Invoice

        rows = Invoice.objects.filter(tenancy__guest=self).exclude(status__counts_as_settled=True)
        return sum((inv.balance for inv in rows), Decimal('0'))

    def advance_credit(self):
        from billing.models import Payment

        total = Payment.objects.filter(
            tenancy__guest=self, invoice__isnull=True, is_reversed=False
        ).aggregate(s=models.Sum('amount'))['s'] or Decimal('0')
        used = Payment.objects.filter(
            tenancy__guest=self, is_reversed=False, from_credit=True
        ).aggregate(s=models.Sum('amount'))['s'] or Decimal('0')
        return total - used

    def expiring_documents(self, days=30):
        horizon = timezone.localdate() + timezone.timedelta(days=days)
        return self.documents.filter(expiry_date__isnull=False, expiry_date__lte=horizon)


class GuestDocument(TenantScopedModel):
    TYPE_PASSPORT = 'passport'
    TYPE_EMIRATES_ID = 'emirates_id'
    TYPE_VISA = 'visa'
    TYPE_OTHER = 'other'
    TYPE_CHOICES = [
        (TYPE_PASSPORT, _('Passport')),
        (TYPE_EMIRATES_ID, _('Emirates ID')),
        (TYPE_VISA, _('Visa')),
        (TYPE_OTHER, _('Other document')),
    ]

    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name='documents')
    doc_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    number = models.CharField(max_length=60)
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    scan = models.FileField(upload_to='documents/', blank=True, null=True)
    notes = models.CharField(max_length=160, blank=True)
    last_alert_day_bucket = models.PositiveIntegerField(
        null=True, blank=True, help_text=_('Smallest alert window already sent (30 / 15 / 7).')
    )

    class Meta:
        ordering = ['expiry_date']
        indexes = [models.Index(fields=['tenant', 'expiry_date'])]

    def __str__(self):
        return f'{self.guest.full_name} — {self.get_doc_type_display()} {self.number}'

    @property
    def days_to_expiry(self):
        if not self.expiry_date:
            return None
        return (self.expiry_date - timezone.localdate()).days

    @property
    def is_expired(self):
        days = self.days_to_expiry
        return days is not None and days < 0


class GuestInvite(TenantScopedModel):
    """A one-link invitation for a guest to fill in their own profile.

    The guest never gets an account. The link carries a long random token, dies
    on a date, and can be revoked. It shows that guest's own details and nothing
    else in the system.
    """

    DEFAULT_DAYS = 7

    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name='invites')
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='guest_invites',
    )
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    opened_count = models.PositiveIntegerField(default=0)
    last_opened_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{_("Profile link for")} {self.guest.full_name}'

    @classmethod
    def issue(cls, *, guest, user, days=DEFAULT_DAYS):
        """A new link replaces any earlier one, so an old share stops working."""
        cls.objects.filter(guest=guest, revoked_at__isnull=True).update(
            revoked_at=timezone.now())
        return cls.objects.create(
            tenant=guest.tenant, guest=guest, created_by=user,
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timezone.timedelta(days=days),
        )

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_usable(self):
        return not self.is_expired and not self.is_revoked

    @property
    def days_left(self):
        return max((self.expires_at - timezone.now()).days, 0)

    def path(self):
        from django.urls import reverse

        return reverse('guest_profile', args=[self.token])

    def build_url(self, request=None):
        path = self.path()
        return request.build_absolute_uri(path) if request else path

    def mark_opened(self):
        self.opened_count += 1
        self.last_opened_at = timezone.now()
        self.save(update_fields=['opened_count', 'last_opened_at', 'updated_at'])


class Tenancy(TenantScopedModel):
    """One stay: a guest in a bed (or a whole room / flat) from a date, at a rent."""

    LIVE_STATUS_CODES = ['active', 'notice']

    MODE_ADVANCE = 'advance'
    MODE_POSTPAID = 'postpaid'
    RENT_MODE_CHOICES = [
        (MODE_ADVANCE, _('Pay in advance — invoice at the start of the period')),
        (MODE_POSTPAID, _('Post-paid — invoice at the end of the period')),
    ]

    CYCLE_MONTHLY = 'monthly'
    CYCLE_WEEKLY = 'weekly'
    CYCLE_DAILY = 'daily'
    CYCLE_CHOICES = [
        (CYCLE_MONTHLY, _('Monthly')),
        (CYCLE_WEEKLY, _('Weekly')),
        (CYCLE_DAILY, _('Daily')),
    ]

    guest = models.ForeignKey(Guest, on_delete=models.PROTECT, related_name='tenancies')
    bed = models.ForeignKey(
        Bed, on_delete=models.PROTECT, null=True, blank=True, related_name='tenancies',
        help_text=_('Empty when the whole room or flat is let.'),
    )
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name='tenancies')
    apartment = models.ForeignKey(
        'properties.Apartment', on_delete=models.PROTECT, related_name='tenancies',
        null=True, blank=True,
    )
    floor = models.ForeignKey(Floor, on_delete=models.PROTECT, related_name='tenancies')
    building = models.ForeignKey(Building, on_delete=models.PROTECT, related_name='tenancies')

    check_in_date = models.DateField()
    expected_check_out = models.DateField(null=True, blank=True)
    actual_check_out = models.DateField(null=True, blank=True)
    notice_given_on = models.DateField(null=True, blank=True)

    monthly_rent = models.DecimalField(max_digits=10, decimal_places=2)
    billing_cycle = models.CharField(max_length=10, choices=CYCLE_CHOICES, default=CYCLE_MONTHLY)
    billing_day = models.PositiveSmallIntegerField(
        default=1, help_text=_('Day of the month the invoice is raised (1-28).')
    )
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))

    rent_mode = models.CharField(
        _('billing arrangement'), max_length=10, choices=RENT_MODE_CHOICES,
        default=MODE_ADVANCE,
    )
    prorate_first_period = models.BooleanField(
        _('charge only the days stayed in the first period'), default=True,
        help_text=_('Switch off to charge the full rent however late in the month '
                    'they moved in.'),
    )
    first_invoice_amount = models.DecimalField(
        _('agreed joining amount'), max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=_('Overrides the calculation for the first invoice only.'),
    )

    status = models.ForeignKey(
        Status, on_delete=models.PROTECT, null=True, blank=True,
        limit_choices_to={'kind': Status.KIND_TENANCY}, related_name='tenancies',
    )
    agreement_note = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tenancies_opened',
    )
    checked_out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tenancies_closed',
    )

    class Meta:
        ordering = ['-check_in_date']
        verbose_name_plural = _('tenancies')
        indexes = [
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['tenant', 'check_in_date']),
        ]

    def __str__(self):
        return f'{self.guest.full_name} @ {self.location_label()}'

    def location_label(self):
        where = [self.building.name, self.floor.name]
        if self.apartment_id:
            where.append(self.apartment.name)
        where.append(self.room.number)
        if self.bed_id:
            where.append(self.bed.label)
        return ' / '.join(where)

    @property
    def is_live(self):
        return bool(self.status and self.status.code in self.LIVE_STATUS_CODES)

    @property
    def bills_in_advance(self):
        return self.rent_mode == self.MODE_ADVANCE

    def due_date_for(self, period_start, period_end):
        """Advance rent falls due as the period opens; post-paid as it closes."""
        return period_start if self.bills_in_advance else period_end

    def billing_period_for(self, on_date):
        """The period to invoice when the nightly job runs on ``on_date``.

        Paying in advance bills the period that is starting. Post-paid bills the
        one that has just finished.
        """
        if self.bills_in_advance:
            return self.period_for(on_date)
        current_start, _current_end = self.period_for(on_date)
        return self.period_for(current_start - timezone.timedelta(days=1))

    def period_length(self, anchor_date=None):
        """How many days one full rent covers for the period around ``anchor_date``.

        For a monthly tenancy that is the real length of that month — 28, 30 or
        31 — not a flat 30. Dividing a month's rent by 30 and charging 31 days
        hands the owner an extra day's rent every long month, and short-changes
        them every February.
        """
        if self.billing_cycle != self.CYCLE_MONTHLY:
            # Weekly and daily cycles scale a monthly figure, so 30 is the base.
            return 30
        start, end = self.period_for(anchor_date or self.check_in_date)
        return (end - start).days + 1

    def daily_rate(self, anchor_date=None):
        """Indicative only — never multiply this out, see ``prorata``."""
        return (self.monthly_rent / Decimal(self.period_length(anchor_date))).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)

    def clean(self):
        super().clean()
        if self.bed_id and self.room_id and self.bed.room_id != self.room_id:
            raise ValidationError({'bed': _('That bed is not in the selected room.')})
        if self.expected_check_out and self.expected_check_out < self.check_in_date:
            raise ValidationError({'expected_check_out': _('Check-out cannot be before check-in.')})
        if self.billing_day < 1 or self.billing_day > 28:
            raise ValidationError({'billing_day': _('Use a day between 1 and 28.')})
        if self.bed_id:
            clash = (
                Tenancy.objects.filter(bed=self.bed, status__code__in=self.LIVE_STATUS_CODES)
                .exclude(pk=self.pk)
                .first()
            )
            if clash:
                raise ValidationError({'bed': _('%(guest)s is already checked in to this bed.') % {
                    'guest': clash.guest.full_name}})

    def save(self, *args, **kwargs):
        if self.room_id:
            self.apartment = self.room.apartment
            self.floor = self.room.apartment.floor
            self.building = self.room.apartment.floor.building
        if self.status_id is None and self.tenant_id:
            self.status = Status.by_code(self.tenant, Status.KIND_TENANCY, 'active') \
                or Status.default_for(self.tenant, Status.KIND_TENANCY)
        super().save(*args, **kwargs)

    # --- money ---------------------------------------------------------
    def deposit_held(self):
        rows = self.deposit_transactions.all()
        held = sum((t.amount for t in rows if t.kind == DepositTransaction.KIND_HOLD), Decimal('0'))
        out = sum(
            (t.amount for t in rows if t.kind in (DepositTransaction.KIND_DEDUCT,
                                                  DepositTransaction.KIND_REFUND)),
            Decimal('0'),
        )
        return held - out

    def deposit_shortfall(self):
        return max(self.deposit_amount - self.deposit_held(), Decimal('0'))

    def outstanding(self):
        return sum((inv.balance for inv in self.invoices.exclude(status__counts_as_settled=True)),
                   Decimal('0'))

    def prorata(self, days, period_days=None, anchor_date=None):
        """Rent for ``days`` out of the billing period, rounded exactly once.

        Rounding the daily rate first and then multiplying drifts by a few fils
        on every invoice (800/30 = 26.67, times 15 = 400.05, not 400.00).
        """
        period_days = period_days or self.period_length(anchor_date)
        if not period_days:
            return Decimal('0.00')
        return (self.monthly_rent * Decimal(days) / Decimal(period_days)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)

    def period_for(self, anchor_date):
        """The billing period that contains ``anchor_date``."""
        if self.billing_cycle == self.CYCLE_MONTHLY:
            start = anchor_date.replace(day=min(self.billing_day, 28))
            if anchor_date.day < start.day:
                start = _add_months(start, -1)
            end = _add_months(start, 1) - timezone.timedelta(days=1)
        elif self.billing_cycle == self.CYCLE_WEEKLY:
            start = anchor_date - timezone.timedelta(days=anchor_date.weekday())
            end = start + timezone.timedelta(days=6)
        else:
            start = end = anchor_date
        return start, end


class DepositTransaction(TenantScopedModel):
    """Money in and out of the deposit a guest leaves with the building.

    A deposit is not income: it is the guest's money, held. Taking it in and
    handing it back never touches profit. A *deduction* is different — that is
    money the building keeps, so it posts to the income ledger.
    """

    KIND_HOLD = 'hold'
    KIND_DEDUCT = 'deduct'
    KIND_REFUND = 'refund'
    KIND_CHOICES = [
        (KIND_HOLD, _('Deposit received')),
        (KIND_DEDUCT, _('Deduction')),
        (KIND_REFUND, _('Refunded to guest')),
    ]
    #: Which way the money moves relative to the building.
    MONEY_IN = {KIND_HOLD}
    MONEY_OUT = {KIND_DEDUCT, KIND_REFUND}

    tenancy = models.ForeignKey(Tenancy, on_delete=models.CASCADE, related_name='deposit_transactions')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=200, blank=True)
    happened_on = models.DateField(default=timezone.localdate)
    receipt_number = models.CharField(max_length=40, blank=True)
    mode = models.ForeignKey(
        'billing.PaymentMode', on_delete=models.PROTECT, null=True, blank=True,
        related_name='deposit_transactions',
        help_text=_('How the money came in, or went back out.'),
    )
    reference = models.CharField(max_length=80, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deposit_entries',
    )

    approval = models.CharField(
        max_length=14, default='not_required',
        choices=[
            ('not_required', _('No approval needed')),
            ('pending', _('Waiting for the owner')),
            ('approved', _('Approved')),
            ('rejected', _('Rejected')),
        ],
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deposit_entries_approved',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-happened_on', '-id']
        indexes = [models.Index(fields=['tenant', 'happened_on'])]

    def __str__(self):
        return f'{self.get_kind_display()} {self.amount} — {self.tenancy.guest.full_name}'

    @property
    def guest(self):
        return self.tenancy.guest

    @property
    def is_money_in(self):
        return self.kind in self.MONEY_IN

    @property
    def awaits_approval(self):
        return self.approval == 'pending'

    @property
    def signed_amount(self):
        return self.amount if self.is_money_in else -self.amount

    def receipt_title(self):
        return {
            self.KIND_HOLD: _('DEPOSIT RECEIPT'),
            self.KIND_DEDUCT: _('DEPOSIT DEDUCTION'),
            self.KIND_REFUND: _('DEPOSIT REFUND'),
        }[self.kind]

    def save(self, *args, **kwargs):
        from core.models import Sequence

        if not self.receipt_number and self.tenant_id:
            self.receipt_number = Sequence.next_value(self.tenant, 'deposit', 'DEP')
        super().save(*args, **kwargs)


class BedTransfer(TenantScopedModel):
    """Moving a guest mid-month, with the rent split pro-rata across both beds."""

    tenancy = models.ForeignKey(Tenancy, on_delete=models.CASCADE, related_name='transfers')
    from_bed = models.ForeignKey(
        Bed, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_out'
    )
    to_bed = models.ForeignKey(
        Bed, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_in'
    )
    from_room = models.ForeignKey(
        Room, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_out'
    )
    to_room = models.ForeignKey(
        Room, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_in'
    )
    transfer_date = models.DateField(default=timezone.localdate)
    old_rent = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    new_rent = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    old_days = models.PositiveIntegerField(default=0)
    new_days = models.PositiveIntegerField(default=0)
    old_prorata = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    new_prorata = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    reason = models.CharField(max_length=200, blank=True)
    moved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='bed_transfers',
    )

    class Meta:
        ordering = ['-transfer_date', '-id']

    def __str__(self):
        return f'{self.tenancy.guest.full_name}: {self.from_bed} → {self.to_bed}'


def _add_months(date, months):
    month = date.month - 1 + months
    year = date.year + month // 12
    month = month % 12 + 1
    day = min(date.day, calendar.monthrange(year, month)[1])
    return date.replace(year=year, month=month, day=day)
