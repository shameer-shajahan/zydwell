"""The Building → Floor → Apartment → Room → Bed picker every list shares.

One object reads ``?building=&floor=&apartment=&room=&bed=`` from the request,
works out a consistent chain from it, offers the options for each level, and
narrows any queryset to the deepest place chosen. Lists filter in the database,
across every row — never only the page that happens to be on screen.

The levels cascade. A floor can only be picked inside a building, a room only
inside an apartment, and so on — unless the level above has a single option,
in which case there is nothing to choose and the next level opens straight
away. Changing a level clears everything below it (``app.js`` does that, and
the server drops a stale child that no longer belongs to its parent anyway).
"""
import re
from urllib.parse import urlencode

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

LEVELS = ('building', 'floor', 'apartment', 'room', 'bed')

LABELS = {
    'building': _('Building'),
    'floor': _('Floor'),
    'apartment': _('Apartment'),
    'room': _('Room'),
    'bed': _('Bed'),
}
EVERY = {
    'building': _('All buildings'),
    'floor': _('All floors'),
    'apartment': _('All apartments'),
    'room': _('All rooms'),
    'bed': _('All beds'),
}
PICK_FIRST = {
    'floor': _('Building first'),
    'apartment': _('Floor first'),
    'room': _('Apartment first'),
    'bed': _('Room first'),
}

#: The attribute on each level that points one level up.
PARENT = {'floor': 'building', 'apartment': 'floor', 'room': 'apartment', 'bed': 'room'}

#: Where each level sits relative to a Bed, for filtering the property tree.
BED_PATHS = {
    'building': 'room__apartment__floor__building',
    'floor': 'room__apartment__floor',
    'apartment': 'room__apartment',
    'room': 'room',
    'bed': 'pk',
}


def _natural_key(text):
    """A2 before A10, room 9 before room 10."""
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r'(\d+)', str(text)))


def _place_querysets():
    from properties.models import Apartment, Bed, Building, Floor, Room

    return {
        'building': Building.objects.filter(is_archived=False),
        'floor': Floor.objects.filter(
            is_archived=False, building__is_archived=False,
        ).select_related('building'),
        'apartment': Apartment.objects.filter(
            is_archived=False, floor__is_archived=False,
            floor__building__is_archived=False,
        ).select_related('floor__building'),
        'room': Room.objects.filter(
            is_archived=False, apartment__is_archived=False,
            apartment__floor__is_archived=False,
            apartment__floor__building__is_archived=False,
        ).select_related('apartment__floor__building'),
        'bed': Bed.objects.filter(
            is_archived=False, room__is_archived=False,
            room__apartment__is_archived=False,
            room__apartment__floor__is_archived=False,
            room__apartment__floor__building__is_archived=False,
        ).select_related('room__apartment__floor__building'),
    }


def _option_label(level, obj):
    return {
        'building': lambda: obj.name,
        'floor': lambda: obj.name,
        'apartment': lambda: obj.name,
        'room': lambda: obj.number,
        'bed': lambda: obj.label,
    }[level]()


class LocationFilter:
    """Read, validate and apply the location picker for one request.

    ``depth`` stops the picker early for records that never point at a bed —
    ledger entries end at the room, the occupancy report at the apartment.
    """

    def __init__(self, request, depth='bed'):
        self.user = request.user
        self.tenant = request.tenant
        self.levels = LEVELS[:LEVELS.index(depth) + 1]
        self._assignments = self._load_assignments()
        raw = {level: (request.GET.get(level) or '').strip() for level in self.levels}
        self.chain = self._resolve(raw)
        self.fields = self._build_fields()

    # --- what the user may see -------------------------------------------
    def _load_assignments(self):
        if not getattr(self.user, 'is_staff_member', False):
            return None
        rows = list(self.user.assignments.all())
        return rows or None

    def scoped(self, level):
        """Places at ``level`` inside this client that this user looks after."""
        from properties.models import Apartment

        qs = _place_querysets()[level].filter(tenant=self.tenant)
        if not self._assignments:
            return qs
        rows = self._assignments
        whole_buildings = [a.building_id for a in rows if not a.floor_id and not a.apartment_id]
        floors = [a.floor_id for a in rows if a.floor_id and not a.apartment_id]
        flats = [a.apartment_id for a in rows if a.apartment_id]
        if level == 'building':
            return qs.filter(pk__in={a.building_id for a in rows})
        if level == 'floor':
            return qs.filter(
                Q(building__in=whole_buildings) | Q(pk__in=floors)
                | Q(pk__in=Apartment.objects.filter(pk__in=flats).values('floor_id')))
        prefix = {'apartment': '', 'room': 'apartment__', 'bed': 'room__apartment__'}[level]
        own = 'pk' if level == 'apartment' else prefix.rstrip('_')
        return qs.filter(
            Q(**{f'{prefix}floor__building__in': whole_buildings})
            | Q(**{f'{prefix}floor__in': floors})
            | Q(**{f'{own}__in': flats}))

    # --- reading the request ---------------------------------------------
    def _with_ancestors(self, level, obj):
        chain = {level: obj}
        while level in PARENT:
            obj = getattr(obj, PARENT[level])
            level = PARENT[level]
            chain[level] = obj
        return chain

    def _resolve(self, raw):
        """The chosen places from the deepest one given, walking up.

        When a higher choice disagrees with a lower one (the building was
        changed but the old floor was still in the form), the higher one was
        the later decision, so it wins and everything under it is dropped.
        """
        levels = [level for level in self.levels if raw[level].isdigit()]
        while levels:
            level = levels.pop()
            obj = self.scoped(level).filter(pk=raw[level]).first()
            if obj is None:
                continue
            chain = self._with_ancestors(level, obj)
            clash = [up for up in levels if int(raw[up]) != chain[up].pk]
            if not clash:
                return chain
            levels = levels[:levels.index(clash[-1]) + 1]
        return {}

    def _build_fields(self):
        fields = []
        parent = None          # the place the next level's options hang under
        for index, level in enumerate(self.levels):
            if index == 0:
                options = list(self.scoped(level))
            elif parent is not None:
                options = list(self.scoped(level).filter(**{PARENT[level]: parent}))
                if level in ('room', 'bed'):
                    options.sort(key=lambda obj: _natural_key(_option_label(level, obj)))
            else:
                options = None
            chosen = self.chain.get(level)
            fields.append({
                'name': level,
                'label': LABELS[level],
                'placeholder': EVERY[level] if options is not None else PICK_FIRST[level],
                'value': str(chosen.pk) if chosen else '',
                'options': [(str(obj.pk), _option_label(level, obj)) for obj in options or []],
                'disabled': options is None,
            })
            # Nothing to choose between one option, so the next level opens.
            parent = chosen or (options[0] if options and len(options) == 1 else None)
        return fields

    # --- using it -----------------------------------------------------------
    @property
    def deepest(self):
        """``(level, place)`` for the narrowest choice, or ``(None, None)``."""
        for level in reversed(self.levels):
            if level in self.chain:
                return level, self.chain[level]
        return None, None

    @property
    def active(self):
        return bool(self.chain)

    def apply(self, queryset, prefix='', paths=None):
        """Narrow ``queryset`` to the chosen place.

        ``prefix`` reaches the record holding the location fields, e.g.
        ``'tenancy__'`` for invoices; ``paths`` maps each level to its own
        lookup when the names differ (see ``BED_PATHS``).
        """
        level, place = self.deepest
        if level is None:
            return queryset
        field = paths[level] if paths else level
        return queryset.filter(**{f'{prefix}{field}': place.pk})

    def params(self):
        return {level: self.chain[level].pk for level in self.levels if level in self.chain}

    @property
    def querystring(self):
        """``building=1&floor=3`` — for links (exports) that must keep the filter."""
        return urlencode(self.params())
