"""Create a realistic demo client so every screen has something to show."""
import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounting.models import KIND_EXPENSE, Category, LedgerEntry
from accounts.models import StaffAssignment, StaffPermission, User
from billing.services import build_invoice, record_payment
from core.constants import ADMIN_ONLY_MODULES, PERMISSION_MODULES
from core.models import Subscription, Tenant
from core.seed import seed_platform_status_templates, seed_tenant
from guests import services as guest_services
from guests.models import Guest, GuestDocument
from properties.models import Apartment, Bed, Building, Floor, Room, RoomType

NAMES = [
    'Rahul Menon', 'Arun Kumar', 'Sajid Ali', 'Mohammed Faris', 'Bijoy Thomas',
    'Anas Rahman', 'Vishnu Prasad', 'Suresh Nair', 'Imran Shaikh', 'Nithin Joseph',
    'Praveen Das', 'Shibu Varghese', 'Rakesh Pillai', 'Ajmal Hussain', 'Manoj Kurian',
    'Deepak Sharma', 'Farhan Iqbal', 'Jithin Raj', 'Noufal Ummer', 'Sandeep Reddy',
]
NATIONALITIES = ['Indian', 'Pakistani', 'Bangladeshi', 'Filipino', 'Nepali', 'Egyptian']
EMPLOYERS = ['Al Futtaim Logistics', 'Emrill Services', 'Transguard', 'Danube Building',
             'Khansaheb Civil', 'Nakheel Facilities']


class Command(BaseCommand):
    help = 'Seed a demo client with buildings, guests, invoices and payments.'

    def add_arguments(self, parser):
        parser.add_argument('--slug', default='sunrise')
        parser.add_argument('--reset', action='store_true',
                            help='Delete the demo client first and rebuild it.')

    @transaction.atomic
    def handle(self, *args, **options):
        random.seed(11)
        slug = options['slug']
        if options['reset']:
            Tenant.objects.filter(slug=slug).delete()
        if Tenant.objects.filter(slug=slug).exists():
            self.stdout.write(self.style.WARNING(
                f'Demo client "{slug}" already exists. Use --reset to rebuild.'))
            return

        seed_platform_status_templates()
        today = timezone.localdate()

        tenant = Tenant.objects.create(
            name='Sunrise Accommodation LLC', slug=slug,
            contact_person='Akhil G K', contact_email='owner@sunrise.example',
            contact_phone='+971 50 123 4567', currency='AED',
            max_buildings=5, max_rooms=200, max_beds=1000, max_staff_seats=10,
        )
        seed_tenant(tenant, with_subscription=False)
        Subscription.objects.create(
            tenant=tenant, plan='Standard', price=Decimal('499'),
            starts_on=today - timedelta(days=60), expires_on=today + timedelta(days=305),
            last_paid_on=today - timedelta(days=60),
        )

        owner = User.objects.create_user(
            username='owner', password='owner12345', role=User.ROLE_ADMIN, tenant=tenant,
            first_name='Akhil', last_name='G K', email='owner@sunrise.example',
            phone='+971501234567',
        )
        # Convenience login for local demos. Change or remove it before go-live —
        # see docs/RUNBOOK.md section 3.
        User.objects.create_user(
            username='admin', password='admin', role=User.ROLE_ADMIN, tenant=tenant,
            first_name='Demo', last_name='Admin', email='admin@sunrise.example',
        )
        collector = User.objects.create_user(
            username='collector', password='staff12345', role=User.ROLE_STAFF, tenant=tenant,
            first_name='Rajeev', last_name='Nair', phone='+971509876543',
            job_title='Supervisor / rent collector',
        )
        for module, _label in PERMISSION_MODULES:
            if module in ADMIN_ONLY_MODULES:
                continue
            StaffPermission.objects.create(
                user=collector, module=module, can_view=True,
                can_create=module in {'guests', 'billing', 'accounting', 'messaging', 'pos'},
                can_edit=module in {'guests', 'billing'},
            )

        bedspace = RoomType.objects.get(tenant=tenant, code='bedspace')
        partition = RoomType.objects.get(tenant=tenant, code='partition')
        studio = RoomType.objects.get(tenant=tenant, code='studio')
        family = RoomType.objects.get(tenant=tenant, code='family_flat')

        buildings = []
        for index, (name, city, rooms_per_floor, floors) in enumerate([
            ('Sunrise Tower', 'Al Quoz, Dubai', 4, 4),
            ('Marina Residency', 'Al Nahda, Sharjah', 3, 3),
        ]):
            building = Building.objects.create(
                tenant=tenant, name=name, code=f'B{index + 1}', city=city,
                address=f'{name}, {city}', owner_name='Akhil G K',
                licence_number=f'DED-{45000 + index}', licence_expiry=today + timedelta(days=180),
                contact_phone='+971 4 555 010%d' % index,
                receipt_footer='Thank you. Rent is due on the 1st of each month.',
            )
            buildings.append(building)
            for level in range(1, floors + 1):
                floor = Floor.objects.create(
                    tenant=tenant, building=building, name=f'Floor {level}', sort_order=level,
                )
                apartment = Apartment.objects.create(
                    tenant=tenant, floor=floor, name=f'A{level}{index + 1}', sort_order=1,
                )
                for slot in range(1, rooms_per_floor + 1):
                    number = f'{level}0{slot}'
                    if slot == rooms_per_floor and level == floors:
                        room = Room.objects.create(
                            tenant=tenant, apartment=apartment, number=number, room_type=family,
                            capacity=1, rent_basis=Room.RENT_PER_FLAT,
                            monthly_rent=Decimal('6500'), has_attached_bath=True,
                        )
                    elif slot == 1 and level % 2 == 0:
                        room = Room.objects.create(
                            tenant=tenant, apartment=apartment, number=number, room_type=studio,
                            capacity=1, rent_basis=Room.RENT_PER_ROOM,
                            monthly_rent=Decimal('3200'), has_attached_bath=True,
                        )
                    else:
                        capacity = random.choice([4, 6, 6, 8])
                        room = Room.objects.create(
                            tenant=tenant, apartment=apartment, number=number,
                            room_type=partition if capacity == 4 else bedspace,
                            capacity=capacity, rent_basis=Room.RENT_PER_BED,
                        )
                        price = Decimal(random.choice([700, 750, 800, 900, 1000]))
                        for slot_index in range(capacity):
                            Bed.objects.create(
                                tenant=tenant, room=room, label=chr(ord('A') + slot_index),
                                monthly_price=price,
                            )

        StaffAssignment.objects.create(user=collector, building=buildings[0])

        free_beds = list(Bed.objects.filter(tenant=tenant).select_related('room'))
        random.shuffle(free_beds)
        guests = []
        for index, name in enumerate(NAMES):
            guest = Guest.objects.create(
                tenant=tenant, full_name=name,
                mobile='+9715%d%07d' % (random.randint(0, 6), random.randint(1000000, 9999999)),
                nationality=random.choice(NATIONALITIES),
                employer=random.choice(EMPLOYERS),
                emergency_contact_name='Family contact',
                emergency_contact_number='+9715%07d' % random.randint(1000000, 9999999),
                date_of_birth=today - timedelta(days=365 * random.randint(22, 46)),
            )
            guests.append(guest)
            GuestDocument.objects.create(
                tenant=tenant, guest=guest, doc_type=GuestDocument.TYPE_PASSPORT,
                number='P%07d' % random.randint(1000000, 9999999),
                expiry_date=today + timedelta(days=random.choice([9, 14, 27, 200, 640])),
            )
            GuestDocument.objects.create(
                tenant=tenant, guest=guest, doc_type=GuestDocument.TYPE_VISA,
                number='V%07d' % random.randint(1000000, 9999999),
                expiry_date=today + timedelta(days=random.choice([5, 25, 120, 300])),
            )

        tenancies = []
        for index, guest in enumerate(guests):
            bed = free_beds[index]
            started = today - timedelta(days=random.randint(20, 300))
            rent = bed.monthly_price
            tenancy = guest_services.check_in(
                tenant=tenant, user=owner, guest=guest, room=bed.room, bed=bed,
                check_in_date=started, monthly_rent=rent,
                deposit_amount=rent, deposit_received=rent if index % 5 else Decimal('0'),
                billing_day=1,
            )
            tenancies.append(tenancy)

        # A whole-flat let, to exercise the per-flat rent basis.
        flat = Room.objects.filter(tenant=tenant, rent_basis=Room.RENT_PER_FLAT).first()
        if flat:
            family_guest = Guest.objects.create(
                tenant=tenant, full_name='Sameer Abdulla', mobile='+971501112233',
                nationality='Emirati', employer='Self employed',
            )
            tenancies.append(guest_services.check_in(
                tenant=tenant, user=owner, guest=family_guest, room=flat, bed=None,
                check_in_date=today - timedelta(days=95), monthly_rent=flat.monthly_rent,
                deposit_amount=flat.monthly_rent, deposit_received=flat.monthly_rent,
                billing_day=1,
            ))

        invoices = []
        for tenancy in tenancies:
            anchor = tenancy.check_in_date
            for _step in range(4):
                start, end = tenancy.period_for(anchor)
                if start > today:
                    break
                effective = max(start, tenancy.check_in_date)
                invoices.append(build_invoice(
                    tenancy, period_start=effective, period_end=end, due_date=start,
                    user=owner, prorata=effective > start,
                ))
                anchor = end + timedelta(days=1)

        collectors = [owner, collector]
        for invoice in invoices:
            roll = random.random()
            if roll < 0.62:
                amount = invoice.total
            elif roll < 0.8:
                amount = (invoice.total / 2).quantize(Decimal('0.01'))
            else:
                continue
            record_payment(
                tenant=tenant, user=random.choice(collectors), tenancy=invoice.tenancy,
                amount=amount, invoice=invoice,
                mode=random.choice(['cash', 'cash', 'bank', 'card', 'cheque']),
                reference='CHQ%05d' % random.randint(10000, 99999),
                paid_on=min(invoice.due_date + timedelta(days=random.randint(0, 12)), today),
            )
            invoice.refresh_from_db()

        expense_categories = list(Category.objects.filter(tenant=tenant, kind=KIND_EXPENSE))
        for month_back in range(6):
            month_day = (today.replace(day=1) - timedelta(days=30 * month_back)).replace(day=5)
            for category in expense_categories:
                LedgerEntry.objects.create(
                    tenant=tenant, kind=KIND_EXPENSE, category=category,
                    amount=Decimal(random.randint(600, 9000)),
                    entry_date=month_day, description=f'{category.name} — monthly',
                    building=random.choice(buildings), payee='Vendor',
                    recorded_by=owner,
                )

        for invoice in invoices[:4]:
            invoice.apply_late_fee()

        self.stdout.write(self.style.SUCCESS('Demo client ready.'))
        self.stdout.write(f'  Client        : {tenant.name} ({tenant.slug})')
        self.stdout.write('  Admin login   : admin / admin   (demo only — change before go-live)')
        self.stdout.write('  Owner login   : owner / owner12345')
        self.stdout.write('  Staff login   : collector / staff12345')
        self.stdout.write(f'  Buildings {len(buildings)} · beds {Bed.objects.filter(tenant=tenant).count()}'
                          f' · guests {len(guests) + 1} · invoices {len(invoices)}')
