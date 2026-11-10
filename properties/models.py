"""Building → Floor → Apartment → Room → Bed."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from core.models import Status, TenantScopedModel


class Building(TenantScopedModel):
    name = models.CharField(_('building name'), max_length=150)
    code = models.CharField(max_length=20, blank=True, help_text=_('Short code used on receipts.'))
    address = models.TextField(blank=True)
    city = models.CharField(max_length=80, blank=True)
    owner_name = models.CharField(max_length=150, blank=True)
    licence_number = models.CharField(max_length=80, blank=True)
    licence_expiry = models.DateField(null=True, blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    contact_email = models.EmailField(blank=True)
    logo = models.ImageField(upload_to='buildings/', blank=True, null=True)
    receipt_footer = models.CharField(max_length=200, blank=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def bed_queryset(self):
        return Bed.objects.filter(room__apartment__floor__building=self,
                                  is_archived=False)

    def occupancy(self):
        return occupancy_for(self.bed_queryset)

    def can_delete(self):
        from guests.models import Tenancy

        if Tenancy.objects.filter(building=self,
                                  status__code__in=Tenancy.LIVE_STATUS_CODES).exists():
            return False, _('A guest is still checked in to this building.')
        # A building owns its complete property tree. Delete that tree together
        # when it has no history; otherwise keep the tree available by archiving
        # the building as a whole.
        places = [self]
        floors = list(self.floors.all())
        places.extend(floors)
        apartments = list(Apartment.objects.filter(floor__in=floors))
        places.extend(apartments)
        places.extend(Room.objects.filter(apartment__in=apartments))
        for place in places:
            allowed, reason = _history_blocks_delete(place)
            if not allowed:
                return False, reason
        return True, ''


class Floor(TenantScopedModel):
    building = models.ForeignKey(Building, on_delete=models.CASCADE, related_name='floors')
    name = models.CharField(_('floor name or number'), max_length=60)
    sort_order = models.PositiveIntegerField(default=0)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ['building__name', 'sort_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['building', 'name'], name='uniq_floor_name_per_building'),
        ]

    def __str__(self):
        return f'{self.building.name} · {self.name}'

    @property
    def bed_queryset(self):
        return Bed.objects.filter(room__apartment__floor=self, is_archived=False)

    def occupancy(self):
        return occupancy_for(self.bed_queryset)

    def can_delete(self):
        from guests.models import Tenancy

        if Tenancy.objects.filter(floor=self,
                                  status__code__in=Tenancy.LIVE_STATUS_CODES).exists():
            return False, _('A guest is still checked in on this floor.')
        if self.apartments.filter(is_archived=False).exists():
            return False, _('Remove or archive the apartments on this floor first.')
        return _history_blocks_delete(self)


class Apartment(TenantScopedModel):
    """A flat on a floor. Rooms live inside it, beds inside the rooms."""

    floor = models.ForeignKey(Floor, on_delete=models.CASCADE, related_name='apartments')
    name = models.CharField(_('apartment name or number'), max_length=60)
    notes = models.CharField(max_length=160, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ['floor__building__name', 'floor__sort_order', 'sort_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['floor', 'name'], name='uniq_apartment_per_floor'),
        ]

    def __str__(self):
        return f'{self.floor.building.name} · {self.floor.name} · {self.name}'

    def short_label(self):
        return f'{self.floor.name} / {self.name}'

    @property
    def building(self):
        return self.floor.building

    @property
    def bed_queryset(self):
        return Bed.objects.filter(room__apartment=self, is_archived=False)

    def occupancy(self):
        return occupancy_for(self.bed_queryset)

    def can_delete(self):
        from guests.models import Tenancy

        if Tenancy.objects.filter(apartment=self,
                                  status__code__in=Tenancy.LIVE_STATUS_CODES).exists():
            return False, _('A guest is still checked in to this apartment.')
        if self.rooms.filter(is_archived=False).exists():
            return False, _('Remove or archive the rooms in this apartment first.')
        return _history_blocks_delete(self)


class RoomType(TenantScopedModel):
    """Bedspace / partition / studio / family flat — editable per client."""

    name = models.CharField(max_length=60)
    code = models.SlugField()
    description = models.CharField(max_length=160, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'code'], name='uniq_roomtype_per_tenant'),
        ]

    def __str__(self):
        return self.name


class Room(TenantScopedModel):
    RENT_PER_BED = 'per_bed'
    RENT_PER_ROOM = 'per_room'
    RENT_PER_FLAT = 'per_flat'
    RENT_BASIS_CHOICES = [
        (RENT_PER_BED, _('Per bed (bedspace model)')),
        (RENT_PER_ROOM, _('Per room (whole room let)')),
        (RENT_PER_FLAT, _('Per flat (full apartment let)')),
    ]

    apartment = models.ForeignKey(
        Apartment, on_delete=models.CASCADE, related_name='rooms')
    number = models.CharField(_('room number'), max_length=40)
    room_type = models.ForeignKey(
        RoomType, on_delete=models.PROTECT, null=True, blank=True, related_name='rooms'
    )
    capacity = models.PositiveIntegerField(default=1, help_text=_('Maximum number of beds.'))
    rent_basis = models.CharField(max_length=12, choices=RENT_BASIS_CHOICES, default=RENT_PER_BED)
    monthly_rent = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0'),
        help_text=_('Used when the rent basis is per room or per flat.'),
    )
    has_ac = models.BooleanField(default=True)
    has_attached_bath = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ['apartment__floor__building__name', 'apartment__floor__sort_order',
                    'apartment__sort_order', 'number']
        constraints = [
            models.UniqueConstraint(fields=['apartment', 'number'],
                                    name='uniq_room_number_per_apartment'),
        ]

    def __str__(self):
        return (f'{self.apartment.floor.building.name} · {self.apartment.floor.name} · '
                f'{self.apartment.name} · {_("Room")} {self.number}')

    @property
    def floor(self):
        return self.apartment.floor

    @property
    def building(self):
        return self.apartment.floor.building

    @property
    def lets_whole_unit(self):
        return self.rent_basis in {self.RENT_PER_ROOM, self.RENT_PER_FLAT}

    def active_bed_count(self):
        return self.beds.filter(is_archived=False).count()

    def occupancy(self):
        return occupancy_for(self.beds.filter(is_archived=False))

    def clean(self):
        super().clean()
        if self.capacity < 1:
            raise ValidationError({'capacity': _('Capacity must be at least one.')})
        if self.pk:
            live = self.active_bed_count()
            if live > self.capacity:
                raise ValidationError({'capacity': _(
                    'This room already has %(count)d beds; capacity cannot be lower.'
                ) % {'count': live}})

    def can_delete(self):
        """Deleting is blocked while anyone is living here — archive instead."""
        from guests.models import Tenancy

        if Tenancy.objects.filter(room=self, status__code__in=Tenancy.LIVE_STATUS_CODES).exists():
            return False, _('A guest is still checked in to this room.')
        if self.beds.filter(is_archived=False).exists():
            return False, _('Remove or archive the beds in this room first.')
        return _history_blocks_delete(self)


class Bed(TenantScopedModel):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='beds')
    label = models.CharField(_('bed label'), max_length=40)
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    status = models.ForeignKey(
        Status, on_delete=models.PROTECT, null=True, blank=True,
        limit_choices_to={'kind': Status.KIND_BED}, related_name='beds',
    )
    is_archived = models.BooleanField(default=False)
    notes = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ['room__apartment__floor__sort_order', 'room__apartment__sort_order',
                    'room__number', 'label']
        constraints = [
            models.UniqueConstraint(fields=['room', 'label'], name='uniq_bed_label_per_room'),
        ]

    def __str__(self):
        return f'{self.room.number}-{self.label}'

    def full_label(self):
        apartment = self.room.apartment
        return (f'{apartment.floor.building.name} · {apartment.floor.name} · '
                f'{apartment.name} · {self.room.number} · {self.label}')

    @property
    def apartment(self):
        return self.room.apartment

    @property
    def floor(self):
        return self.room.apartment.floor

    @property
    def building(self):
        return self.room.apartment.floor.building

    @property
    def is_occupied(self):
        return bool(self.status and self.status.counts_as_occupied)

    @property
    def is_available(self):
        return bool(self.status and self.status.counts_as_available)

    def current_tenancy(self):
        from guests.models import Tenancy

        return (
            Tenancy.objects.filter(bed=self, status__code__in=Tenancy.LIVE_STATUS_CODES)
            .select_related('guest')
            .first()
        )

    def clean(self):
        super().clean()
        if self.room_id and not self.is_archived:
            siblings = Bed.objects.filter(room=self.room, is_archived=False).exclude(pk=self.pk)
            if siblings.count() + 1 > self.room.capacity:
                raise ValidationError(_(
                    'Room %(room)s allows %(cap)d bed(s); it already has %(count)d.'
                ) % {'room': self.room.number, 'cap': self.room.capacity, 'count': siblings.count()})

    def save(self, *args, **kwargs):
        if self.status_id is None and self.tenant_id:
            self.status = Status.default_for(self.tenant, Status.KIND_BED)
        if not self.monthly_price and self.room_id and self.room.rent_basis == Room.RENT_PER_BED:
            self.monthly_price = self.monthly_price or Decimal('0')
        super().save(*args, **kwargs)

    def can_delete(self):
        from guests.models import Tenancy

        if Tenancy.objects.filter(bed=self, status__code__in=Tenancy.LIVE_STATUS_CODES).exists():
            return False, _('This bed is occupied. Check the guest out or archive the bed instead.')
        if Tenancy.objects.filter(bed=self).exists():
            return False, _('This bed has stay history. Archive it instead of deleting.')
        return True, ''

    def set_status_code(self, code, save=True):
        status = Status.by_code(self.tenant, Status.KIND_BED, code)
        if status:
            self.status = status
            if save:
                self.save(update_fields=['status', 'updated_at'])
        return status


def _history_blocks_delete(place):
    """Past stays and money records keep a place alive — it is archived instead.

    Stays are PROTECTed, so deleting would fail; ledger entries would lose the
    place they were booked against, and recurring expenses would vanish.
    """
    if place.tenancies.exists():
        return False, _('It has stay history.')
    if place.ledger_entries.exists():
        return False, _('It has income or expense entries.')
    if getattr(place, 'recurring_expenses', None) is not None and place.recurring_expenses.exists():
        return False, _('It has recurring expenses.')
    return True, ''


def occupancy_for(bed_queryset):
    """``{total, occupied, available, other, percent}`` for a set of beds.

    Counted by the database in one query rather than by loading every bed.
    """
    from django.db.models import Count

    counts = bed_queryset.order_by().aggregate(
        total=Count('id'),
        occupied=Count('id', filter=Q(status__counts_as_occupied=True)),
        available=Count('id', filter=Q(status__counts_as_available=True)
                        & ~Q(status__counts_as_occupied=True)),
    )
    return occupancy_from_counts(counts['total'], counts['occupied'], counts['available'])


def occupancy_grouped(bed_queryset, by):
    """``{place id: occupancy}`` for every place at one level, in one query.

    ``by`` is the lookup from a bed to the level, e.g.
    ``'room__apartment__floor'``. Counts follow ``Building.occupancy()`` and
    friends: every bed that is not itself archived.
    """
    from django.db.models import Count, F

    return {
        row['place']: occupancy_from_counts(row['total'], row['occupied'], row['available'])
        for row in bed_queryset.filter(is_archived=False).order_by()
        .values(place=F(by)).annotate(
            total=Count('id'),
            occupied=Count('id', filter=Q(status__counts_as_occupied=True)),
            available=Count('id', filter=Q(status__counts_as_available=True)
                            & ~Q(status__counts_as_occupied=True)),
        )
    }


def occupancy_of(beds):
    """The same figures for beds already in memory, statuses loaded."""
    total = occupied = available = 0
    for bed in beds:
        total += 1
        if bed.status and bed.status.counts_as_occupied:
            occupied += 1
        elif bed.status and bed.status.counts_as_available:
            available += 1
    return occupancy_from_counts(total, occupied, available)


def occupancy_from_counts(total, occupied, available):
    other = total - occupied - available
    percent = round((occupied / total) * 100, 1) if total else 0.0
    return {
        'total': total,
        'occupied': occupied,
        'available': available,
        'other': other,
        'percent': percent,
    }


def visible_buildings(user, tenant):
    qs = Building.objects.filter(tenant=tenant, is_archived=False)
    if user.is_staff_member:
        assignments = list(user.assignments.all())
        if assignments:
            ids = {a.building_id for a in assignments}
            qs = qs.filter(pk__in=ids)
    return qs


def visible_beds(user, tenant):
    qs = Bed.objects.filter(
        tenant=tenant, is_archived=False, room__is_archived=False,
        room__apartment__is_archived=False,
        room__apartment__floor__is_archived=False,
        room__apartment__floor__building__is_archived=False,
    ).select_related(
        'room', 'room__apartment', 'room__apartment__floor',
        'room__apartment__floor__building', 'status'
    )
    if user.is_staff_member:
        assignments = list(user.assignments.all())
        if assignments:
            buildings = [a.building_id for a in assignments
                         if a.building_id and not a.floor_id and not a.apartment_id]
            floors = [a.floor_id for a in assignments if a.floor_id and not a.apartment_id]
            apartments = [a.apartment_id for a in assignments if a.apartment_id]
            q = Q(pk__in=[])
            if buildings:
                q |= Q(room__apartment__floor__building_id__in=buildings)
            if floors:
                q |= Q(room__apartment__floor_id__in=floors)
            if apartments:
                q |= Q(room__apartment_id__in=apartments)
            qs = qs.filter(q)
    return qs
