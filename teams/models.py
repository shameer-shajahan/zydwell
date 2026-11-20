"""Team lets: a company rents a whole apartment or one room for its people."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from guests.models import Tenancy


class TeamLetQuerySet(models.QuerySet):
    def live(self):
        return self.filter(status__code__in=Tenancy.LIVE_STATUS_CODES)


class TeamLet(Tenancy):
    """A room or whole apartment let to a company's team, billed as one rent.

    It *is* a tenancy (multi-table inheritance): the parent row carries the
    company (``guest``), the apartment, rent, billing day, deposit and status,
    so invoices, payments, the nightly billing run, overdue lists and reports
    treat it like any other stay. This table adds what only a team has.

    ``bed`` stays empty. ``room`` is empty for a whole-apartment let, or names
    the one room the team takes. Several teams may share one apartment, and the
    headcount is deliberately not checked against the beds.
    """

    team_name = models.CharField(_('team name'), max_length=150)
    joining_date = models.DateField(_('joining date'), null=True, blank=True)
    members = models.PositiveIntegerField(_('members'), default=0)
    per_head = models.DecimalField(_('rate per head'), max_digits=10, decimal_places=2,
                                   null=True, blank=True)

    objects = TeamLetQuerySet.as_manager()

    class Meta:
        ordering = ['building__name', 'apartment__name', 'team_name']
        verbose_name = _('team let')

    def __str__(self):
        place = self.room.number if self.room_id else self.apartment.name
        return f'{self.team_name} @ {place}'

    @property
    def company(self):
        return self.guest

    @property
    def headcount_rent(self):
        """Members × per-head, or ``None`` when no per-head rate is agreed."""
        if self.per_head is None:
            return None
        return (self.per_head * self.members).quantize(Decimal('0.01'))

    @property
    def rent_differs(self):
        expected = self.headcount_rent
        return expected is not None and expected != self.monthly_rent

    def clean(self):
        if self.room_id:
            self.apartment = self.room.apartment
        if self.apartment_id is None:
            raise ValidationError({'apartment': _('Pick an apartment or room for the team.')})
        self.bed = None
        super().clean()

    def save(self, *args, **kwargs):
        if self.room_id:
            self.apartment = self.room.apartment
        self.bed = None
        super().save(*args, **kwargs)
