"""Building, floor, room and bed management, plus the visual bed board."""
import re

from django.contrib import messages
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import audit
from core.constants import MODULE_PROPERTIES, MODULE_SETTINGS
from core.filters import BED_PATHS, LocationFilter
from core.models import AuditLog, Status
from core.permissions import admin_required, require_module
from core.utils import paginate, redirect_back, safe_next

from .forms import (
    ApartmentForm, BedForm, BuildingForm, BulkBedForm, FloorForm, RoomForm,
    RoomTypeForm,
)
from .models import (
    Apartment, Bed, Building, Floor, Room, RoomType, occupancy_for, occupancy_from_counts,
    occupancy_grouped, occupancy_of, visible_beds, visible_buildings,
)

EMPTY = occupancy_from_counts(0, 0, 0)


@require_module(MODULE_PROPERTIES)
def building_list(request):
    buildings = visible_buildings(request.user, request.tenant).annotate(
        floor_count=Count('floors', filter=Q(floors__is_archived=False), distinct=True),
    ).order_by('name')     # GROUP BY drops Meta.ordering; pages need a fixed order
    page = paginate(request, buildings, 24)
    ids = [building.pk for building in page]
    # Every card's figures in two grouped queries, not two per building.
    beds = occupancy_grouped(Bed.objects.filter(room__apartment__floor__building__in=ids),
                             'room__apartment__floor__building')
    rooms = dict(Room.objects.filter(
        apartment__floor__building__in=ids, is_archived=False,
    ).order_by().values_list('apartment__floor__building').annotate(n=Count('id')))
    cards = [{'obj': building,
              'occupancy': beds.get(building.pk, EMPTY),
              'rooms': rooms.get(building.pk, 0)} for building in page]
    return render(request, 'properties/building_list.html', {
        'cards': cards,
        'page': page,
        'can_edit': request.user.can(MODULE_PROPERTIES, 'create'),
    })


@require_module(MODULE_PROPERTIES, 'create')
def building_form(request, pk=None):
    building = get_object_or_404(Building, pk=pk, tenant=request.tenant) if pk else None
    before = audit.snapshot(building) if building else {}
    form = BuildingForm(request.POST or None, request.FILES or None, instance=building,
                        tenant=request.tenant)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        obj.save()
        audit.log(AuditLog.ACTION_UPDATE if building else AuditLog.ACTION_CREATE, obj,
                  summary=_('Building %(name)s saved') % {'name': obj.name},
                  changes=audit.diff(before, audit.snapshot(obj)) if building else {})
        messages.success(request, _('Building saved.'))
        return redirect_back(request, 'properties:building_detail', pk=obj.pk, made=obj)
    return render(request, 'properties/building_form.html', {'form': form, 'building': building})


@require_module(MODULE_PROPERTIES)
def building_detail(request, pk):
    building = get_object_or_404(
        visible_buildings(request.user, request.tenant), pk=pk
    )
    floor_blocks = _plan_blocks(_plan_floors([building]))
    return render(request, 'properties/building_detail.html', {
        'building': building,
        'floor_blocks': floor_blocks,
        'occupancy': building.occupancy(),
        'statuses': Status.objects.filter(tenant=request.tenant, kind=Status.KIND_BED),
        'can_edit': request.user.can(MODULE_PROPERTIES, 'create'),
        'can_delete': request.user.can(MODULE_PROPERTIES, 'delete'),
    })


@require_module(MODULE_PROPERTIES, 'delete')
def building_delete(request, pk):
    building = get_object_or_404(Building, pk=pk, tenant=request.tenant)
    if request.method != 'POST':
        return redirect('properties:building_detail', pk=building.pk)
    allowed, reason = building.can_delete()
    if allowed:
        audit.log(AuditLog.ACTION_DELETE, building,
                  summary=_('Deleted building %(n)s') % {'n': building.name})
        building.delete()
        messages.success(request, _('Building deleted.'))
    else:
        building.is_archived = True
        building.save(update_fields=['is_archived', 'updated_at'])
        audit.log(AuditLog.ACTION_UPDATE, building,
                  summary=_('Archived building %(n)s — %(why)s') % {
                      'n': building.name, 'why': reason})
        messages.warning(request, _('%(why)s The building has been archived instead.') % {
            'why': reason})
    return redirect('properties:building_list')


@require_module(MODULE_PROPERTIES)
def bed_board(request):
    """Floor-wise occupancy map across every building the user can see."""
    location = LocationFilter(request)
    buildings = visible_buildings(request.user, request.tenant)
    if 'building' in location.chain:
        buildings = buildings.filter(pk=location.chain['building'].pk)
    buildings = list(buildings)

    by_building = {}
    for block in _plan_blocks(_plan_floors(buildings, location)):
        by_building.setdefault(block['floor'].building_id, []).append(block)
    stats = occupancy_grouped(Bed.objects.filter(room__apartment__floor__building__in=buildings),
                              'room__apartment__floor__building')
    blocks = [{'building': building,
               'occupancy': stats.get(building.pk, EMPTY),
               'floors': by_building.get(building.pk, [])} for building in buildings]

    return render(request, 'properties/bed_board.html', {
        'blocks': blocks,
        'location': location,
        'statuses': Status.objects.filter(tenant=request.tenant, kind=Status.KIND_BED),
        'overall': occupancy_for(location.apply(
            visible_beds(request.user, request.tenant), paths=BED_PATHS)),
    })


def _plan_floors(buildings, location=None):
    """Floors with their apartments, rooms and beds loaded in four queries.

    ``location`` trims each level to the place picked on the bed board.
    """
    beds = Bed.objects.filter(is_archived=False).select_related('status')
    rooms = Room.objects.filter(is_archived=False).select_related('room_type')
    apartments = Apartment.objects.filter(is_archived=False)
    floors = Floor.objects.filter(building__in=buildings, is_archived=False) \
        .select_related('building')
    chain = location.chain if location is not None else {}
    if 'floor' in chain:
        floors = floors.filter(pk=chain['floor'].pk)
    if 'apartment' in chain:
        apartments = apartments.filter(pk=chain['apartment'].pk)
    if 'room' in chain:
        rooms = rooms.filter(pk=chain['room'].pk)
    if 'bed' in chain:
        beds = beds.filter(pk=chain['bed'].pk)
    return floors.prefetch_related(
        Prefetch('apartments', queryset=apartments),
        Prefetch('apartments__rooms', queryset=rooms),
        Prefetch('apartments__rooms__beds', queryset=beds),
    )


def _plan_blocks(floors):
    """The floor plan as nested dicts, with live stays fetched in one query."""
    from guests.models import Tenancy

    floors = list(floors)
    room_ids = [room.pk for floor in floors for flat in floor.apartments.all()
                for room in flat.rooms.all()]
    # Floor and apartment badges count exactly as Floor.occupancy() and
    # Apartment.occupancy() do — two grouped queries for the whole plan.
    by_floor = occupancy_grouped(Bed.objects.filter(room__apartment__floor__in=floors),
                                 'room__apartment__floor')
    by_flat = occupancy_grouped(
        Bed.objects.filter(room__apartment__floor__in=floors), 'room__apartment')
    stays = {}
    for tenancy in Tenancy.objects.filter(
        room_id__in=room_ids, status__code__in=Tenancy.LIVE_STATUS_CODES,
    ).select_related('guest'):
        stays.setdefault(tenancy.room_id, {})[tenancy.bed_id] = tenancy

    blocks = []
    for floor in floors:
        flats = []
        for apartment in floor.apartments.all():
            rows = []
            for room in apartment.rooms.all():
                beds = sorted(room.beds.all(), key=lambda bed: _natural_label_key(bed.label))
                live = stays.get(room.pk, {})
                rows.append({
                    'obj': room,
                    'beds': [{'obj': bed, 'tenancy': live.get(bed.pk)} for bed in beds],
                    'unit_tenancy': live.get(None),
                    'occupancy': occupancy_of(beds),
                })
            flats.append({'apartment': apartment, 'rooms': rows,
                          'occupancy': by_flat.get(apartment.pk, EMPTY)})
        blocks.append({'floor': floor, 'apartments': flats,
                       'occupancy': by_floor.get(floor.pk, EMPTY)})
    return blocks


def _natural_label_key(label):
    """Sort labels like A1, A2, A10 in human numeric order."""
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r'(\d+)', label))


@require_module(MODULE_PROPERTIES, 'create')
def floor_form(request, pk=None):
    floor = get_object_or_404(Floor, pk=pk, tenant=request.tenant) if pk else None
    initial = {}
    if request.GET.get('building'):
        initial['building'] = request.GET['building']
    form = FloorForm(request.POST or None, instance=floor, tenant=request.tenant, initial=initial)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        obj.save()
        audit.log(AuditLog.ACTION_UPDATE if floor else AuditLog.ACTION_CREATE, obj,
                  summary=_('Floor %(name)s saved') % {'name': obj.name})
        messages.success(request, _('Floor saved.'))
        return redirect_back(request, 'properties:building_detail', pk=obj.building_id, made=obj)
    return render(request, 'properties/simple_form.html', {
        'form': form, 'heading': _('Floor'),
        'subheading': _('A floor, or a single flat / apartment unit.'),
        'cancel_url': 'properties:building_list',
    })


@require_module(MODULE_PROPERTIES, 'delete')
def floor_delete(request, pk):
    floor = get_object_or_404(Floor, pk=pk, tenant=request.tenant)
    building_id = floor.building_id
    if request.method == 'POST':
        allowed, reason = floor.can_delete()
        if allowed:
            audit.log(AuditLog.ACTION_DELETE, floor,
                      summary=_('Deleted floor %(n)s') % {'n': floor.name})
            floor.delete()
            messages.success(request, _('Floor deleted.'))
        else:
            floor.is_archived = True
            floor.save(update_fields=['is_archived', 'updated_at'])
            audit.log(AuditLog.ACTION_UPDATE, floor,
                      summary=_('Archived floor %(n)s — %(why)s') % {
                          'n': floor.name, 'why': reason})
            messages.warning(request, _('%(why)s The floor has been archived instead.') % {
                'why': reason})
    return redirect('properties:building_detail', pk=building_id)


@require_module(MODULE_PROPERTIES, 'create')
def apartment_form(request, pk=None):
    apartment = get_object_or_404(Apartment, pk=pk, tenant=request.tenant) if pk else None
    before = audit.snapshot(apartment) if apartment else {}
    initial = {}
    if request.GET.get('floor'):
        initial['floor'] = request.GET['floor']
    form = ApartmentForm(request.POST or None, instance=apartment, tenant=request.tenant,
                         initial=initial)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        obj.save()
        audit.log(AuditLog.ACTION_UPDATE if apartment else AuditLog.ACTION_CREATE, obj,
                  summary=_('Apartment %(name)s saved') % {'name': obj.name},
                  changes=audit.diff(before, audit.snapshot(obj)) if apartment else {})
        messages.success(request, _('Apartment saved.'))
        return redirect_back(request, 'properties:building_detail', pk=obj.floor.building_id,
                             made=obj)
    return render(request, 'properties/simple_form.html', {
        'form': form, 'heading': _('Apartment'),
        'subheading': _('A flat on a floor. Rooms go inside it.'),
        'cancel_url': 'properties:building_list',
    })


@require_module(MODULE_PROPERTIES, 'delete')
def apartment_delete(request, pk):
    apartment = get_object_or_404(Apartment, pk=pk, tenant=request.tenant)
    building_id = apartment.floor.building_id
    if request.method == 'POST':
        allowed, reason = apartment.can_delete()
        if allowed:
            audit.log(AuditLog.ACTION_DELETE, apartment,
                      summary=_('Deleted apartment %(n)s') % {'n': apartment.name})
            apartment.delete()
            messages.success(request, _('Apartment deleted.'))
        else:
            apartment.is_archived = True
            apartment.save(update_fields=['is_archived', 'updated_at'])
            audit.log(AuditLog.ACTION_UPDATE, apartment,
                      summary=_('Archived apartment %(n)s — %(why)s') % {
                          'n': apartment.name, 'why': reason})
            messages.warning(request, _('%(why)s It has been archived instead.') % {
                'why': reason})
    return redirect('properties:building_detail', pk=building_id)


@require_module(MODULE_PROPERTIES, 'create')
def room_form(request, pk=None):
    room = get_object_or_404(Room, pk=pk, tenant=request.tenant) if pk else None
    before = audit.snapshot(room) if room else {}
    initial = {}
    if request.GET.get('apartment'):
        initial['apartment'] = request.GET['apartment']
    form = RoomForm(request.POST or None, instance=room, tenant=request.tenant, initial=initial)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        try:
            obj.full_clean(exclude=['tenant'])
        except Exception as exc:
            messages.error(request, str(exc))
        else:
            obj.save()
            audit.log(AuditLog.ACTION_UPDATE if room else AuditLog.ACTION_CREATE, obj,
                      summary=_('Room %(no)s saved') % {'no': obj.number},
                      changes=audit.diff(before, audit.snapshot(obj)) if room else {})
            messages.success(request, _('Room saved.'))
            return redirect_back(request, 'properties:room_detail', pk=obj.pk, made=obj)
    return render(request, 'properties/simple_form.html', {
        'form': form, 'heading': _('Room'),
        'subheading': _('Bedspace, partition, studio or family flat.'),
        'cancel_url': 'properties:building_list',
    })


@require_module(MODULE_PROPERTIES)
def room_detail(request, pk):
    room = get_object_or_404(
        Room.objects.select_related('apartment', 'apartment__floor',
                                    'apartment__floor__building', 'room_type'),
        pk=pk, tenant=request.tenant,
    )
    beds = sorted(
        room.beds.filter(is_archived=False).select_related('status'),
        key=lambda bed: _natural_label_key(bed.label),
    )
    bed_count = len(beds)
    bulk_form = BulkBedForm(room=room, initial={
        'monthly_price': room.monthly_rent or 0,
        'start_letter': chr(ord('A') + bed_count),
        'count': max(room.capacity - bed_count, 1),
    })
    if request.method == 'POST' and request.user.can(MODULE_PROPERTIES, 'create'):
        bulk_form = BulkBedForm(request.POST, room=room)
        if bulk_form.is_valid():
            made = _create_beds(request, room, bulk_form.cleaned_data)
            messages.success(request, _('%(n)d bed(s) added.') % {'n': made})
            return redirect('properties:room_detail', pk=room.pk)

    from guests.models import Tenancy

    live = {
        t.bed_id: t for t in Tenancy.objects.filter(
            room=room, status__code__in=Tenancy.LIVE_STATUS_CODES
        ).select_related('guest')
    }
    return render(request, 'properties/room_detail.html', {
        'room': room,
        'beds': [{'obj': b, 'tenancy': live.get(b.pk)} for b in beds],
        'unit_tenancy': live.get(None),
        'bulk_form': bulk_form,
        'occupancy': occupancy_for(room.beds.filter(is_archived=False)),
        'history': Tenancy.objects.filter(room=room).select_related('guest', 'status')[:10],
        'can_edit': request.user.can(MODULE_PROPERTIES, 'create'),
        'can_delete': request.user.can(MODULE_PROPERTIES, 'delete'),
    })


def _create_beds(request, room, data):
    start = ord(data['start_letter'].upper()[0])
    made = 0
    for index in range(data['count']):
        label = chr(start + index)
        if Bed.objects.filter(room=room, label=label).exists():
            continue
        bed = Bed(tenant=request.tenant, room=room, label=label,
                  monthly_price=data['monthly_price'])
        try:
            bed.full_clean(exclude=['tenant', 'status'])
        except Exception as exc:
            messages.error(request, str(exc))
            break
        bed.save()
        made += 1
    if made:
        audit.log(AuditLog.ACTION_CREATE, room,
                  summary=_('Added %(n)d bed(s) to room %(no)s') % {'n': made, 'no': room.number})
    return made


@require_module(MODULE_PROPERTIES, 'create')
def bed_form(request, pk=None):
    bed = get_object_or_404(Bed, pk=pk, tenant=request.tenant) if pk else None
    before = audit.snapshot(bed) if bed else {}
    initial = {}
    if request.GET.get('room'):
        initial['room'] = request.GET['room']
    form = BedForm(request.POST or None, instance=bed, tenant=request.tenant, initial=initial)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        try:
            obj.full_clean(exclude=['tenant'])
        except Exception as exc:
            messages.error(request, str(exc))
        else:
            obj.save()
            audit.log(AuditLog.ACTION_UPDATE if bed else AuditLog.ACTION_CREATE, obj,
                      summary=_('Bed %(label)s saved') % {'label': obj.full_label()},
                      changes=audit.diff(before, audit.snapshot(obj)) if bed else {})
            messages.success(request, _('Bed saved.'))
            return redirect_back(request, 'properties:room_detail', pk=obj.room_id, made=obj)
    return render(request, 'properties/simple_form.html', {
        'form': form, 'heading': _('Bed'),
        'subheading': _('A bed belongs to exactly one room.'),
        'cancel_url': 'properties:building_list',
    })


@require_module(MODULE_PROPERTIES, 'edit')
def bed_status(request, pk):
    bed = get_object_or_404(Bed, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        status = Status.objects.filter(
            pk=request.POST.get('status'), tenant=request.tenant, kind=Status.KIND_BED
        ).first()
        if status:
            before = str(bed.status)
            bed.status = status
            bed.save(update_fields=['status', 'updated_at'])
            audit.log(AuditLog.ACTION_UPDATE, bed,
                      summary=_('Bed %(label)s status changed') % {'label': bed.full_label()},
                      changes={'status': [before, status.name]})
            messages.success(request, _('Bed status updated.'))
    # safe_next, not a bare startswith('/'): "//other.site" also starts with "/".
    next_url = safe_next(request)
    if next_url:
        return redirect(next_url)
    return redirect('properties:room_detail', pk=bed.room_id)


@require_module(MODULE_PROPERTIES, 'delete')
def bed_delete(request, pk):
    bed = get_object_or_404(Bed, pk=pk, tenant=request.tenant)
    room_id = bed.room_id
    if request.method == 'POST':
        allowed, reason = bed.can_delete()
        if allowed:
            audit.log(AuditLog.ACTION_DELETE, bed,
                      summary=_('Deleted bed %(label)s') % {'label': bed.full_label()})
            bed.delete()
            messages.success(request, _('Bed deleted.'))
        else:
            bed.is_archived = True
            bed.save(update_fields=['is_archived', 'updated_at'])
            audit.log(AuditLog.ACTION_UPDATE, bed,
                      summary=_('Archived bed %(label)s — %(why)s') % {
                          'label': bed.full_label(), 'why': reason})
            messages.warning(request, _('%(why)s The bed has been archived instead.') % {'why': reason})
    return redirect('properties:room_detail', pk=room_id)


@require_module(MODULE_PROPERTIES, 'delete')
def room_delete(request, pk):
    room = get_object_or_404(Room, pk=pk, tenant=request.tenant)
    building_id = room.apartment.floor.building_id
    if request.method == 'POST':
        allowed, reason = room.can_delete()
        if allowed:
            audit.log(AuditLog.ACTION_DELETE, room,
                      summary=_('Deleted room %(no)s') % {'no': room.number})
            room.delete()
            messages.success(request, _('Room deleted.'))
        else:
            room.is_archived = True
            room.save(update_fields=['is_archived', 'updated_at'])
            audit.log(AuditLog.ACTION_UPDATE, room,
                      summary=_('Archived room %(no)s — %(why)s') % {'no': room.number, 'why': reason})
            messages.warning(request, _('%(why)s The room has been archived instead.') % {'why': reason})
    return redirect('properties:building_detail', pk=building_id)


@admin_required
def room_type_list(request):
    form = RoomTypeForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = request.tenant
        obj.save()
        audit.log(AuditLog.ACTION_CREATE, obj, summary=_('Room type %(n)s added') % {'n': obj.name})
        messages.success(request, _('Room type added.'))
        return redirect('properties:room_type_list')
    return render(request, 'properties/room_types.html', {
        'form': form,
        'types': paginate(request, RoomType.objects.filter(
            tenant=request.tenant).annotate(n=Count('rooms')), 25),
    })
