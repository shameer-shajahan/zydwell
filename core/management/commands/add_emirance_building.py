"""Add the Naseem building layout supplied for the Emirance account."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from core.models import Tenant
from properties.models import Apartment, Bed, Building, Floor, Room, RoomType


LAYOUT = {
    '101': {'A': 4, 'B': 8},
    '102': {'A': 12, 'B': 8, 'C': 6},
    '103': {'A': 12, 'B': 10, 'C': 8},
    '104': {'A': 12, 'B': 10, 'C': 8},
    '105': {'A': 12, 'B': 8, 'C': 8},
    '106': {'A': 8, 'B': 6},
    '107': {'A': 10, 'B': 6, 'C': 6},
    '108': {'A': 8, 'B': 8},
    '109': {'A': 10, 'B': 8},
    '110': {'A': 10, 'B': 6, 'C': 6},
    '111': {'A': 8, 'B': 6},
    '112': {'A': 12, 'B': 8, 'C': 8},
    '113': {'A': 12, 'B': 8, 'C': 8},
    '114': {'A': 12, 'B': 8, 'C': 8},
    '115': {'A': 14, 'B': 8, 'C': 8},
    '116': {'A': 12, 'B': 8, 'C': 6},
    '117': {'A': 12, 'B': 8, 'C': 6},
}

BUILDING_NAME = 'Naseem Building'
BUILDING_CODE = 'NASEEM'


class Command(BaseCommand):
    help = 'Add the supplied Naseem building layout to the Emirance company.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Create the building, apartments, rooms and beds.',
        )

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(name__iexact='Emirance')
        except Tenant.DoesNotExist as exc:
            raise CommandError('Could not find a company named Emirance.') from exc
        except Tenant.MultipleObjectsReturned as exc:
            raise CommandError('More than one company is named Emirance; refusing to guess.') from exc

        apartment_count = len(LAYOUT)
        room_count = sum(len(rooms) for rooms in LAYOUT.values())
        bed_count = sum(sum(rooms.values()) for rooms in LAYOUT.values())

        if not options['apply']:
            self.stdout.write(
                f'Preview for {tenant.name}: {BUILDING_NAME}, First Floor, '
                f'{apartment_count} apartments, {room_count} rooms, {bed_count} beds. '
                'Bed rents will be AED 0 until entered. Run again with --apply to create.'
            )
            return

        if Building.objects.filter(tenant=tenant).filter(
            Q(name=BUILDING_NAME) | Q(code=BUILDING_CODE)
        ).exists():
            raise CommandError(
                f'{BUILDING_NAME} (code {BUILDING_CODE}) already exists for Emirance; '
                'no records were changed.'
            )

        limits = [
            ('buildings', Building.objects.filter(tenant=tenant, is_archived=False).count(),
             1, tenant.max_buildings),
            ('rooms', Room.objects.filter(tenant=tenant, is_archived=False).count(),
             room_count, tenant.max_rooms),
            ('beds', Bed.objects.filter(tenant=tenant, is_archived=False).count(),
             bed_count, tenant.max_beds),
        ]
        for label, current, additional, limit in limits:
            if current + additional > limit:
                raise CommandError(
                    f'Emirance plan limit exceeded for {label}: '
                    f'{current} existing + {additional} new > {limit} allowed.'
                )

        room_type = RoomType.objects.filter(tenant=tenant, code='bedspace').first()
        if room_type is None:
            raise CommandError('Emirance has no Bedspace room type. No records were changed.')

        with transaction.atomic():
            building = Building.objects.create(
                tenant=tenant, name=BUILDING_NAME, code=BUILDING_CODE,
            )
            floor = Floor.objects.create(
                tenant=tenant, building=building, name='First Floor', sort_order=1,
            )
            for apartment_order, (apartment_name, room_layout) in enumerate(LAYOUT.items(), 1):
                apartment = Apartment.objects.create(
                    tenant=tenant, floor=floor, name=apartment_name,
                    sort_order=apartment_order,
                )
                for room_name, capacity in room_layout.items():
                    room = Room.objects.create(
                        tenant=tenant, apartment=apartment, number=room_name,
                        room_type=room_type, capacity=capacity,
                        rent_basis=Room.RENT_PER_BED,
                    )
                    for number in range(1, capacity + 1):
                        # Use save() so each new bed receives Emirance's default
                        # available status, matching the normal app workflow.
                        Bed.objects.create(
                            tenant=tenant, room=room, label=f'{room_name}{number}',
                            monthly_price=0,
                        )

        self.stdout.write(self.style.SUCCESS(
            f'Created {BUILDING_NAME} for {tenant.name}: 1 floor, {apartment_count} '
            f'apartments, {room_count} rooms, {bed_count} beds.'
        ))
