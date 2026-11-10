"""Empty the database of client data, leaving a usable blank platform.

Deletion order matters: tenancies, invoices and payments protect the records they
point at, so a plain ``Tenant.objects.all().delete()`` raises ProtectedError. The
list below walks children before parents.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

#: (app_label, model, extra filter) — children first.
WIPE_ORDER = [
    ('billing', 'Payment', {}),
    ('billing', 'InvoiceLine', {}),
    ('billing', 'Invoice', {}),
    ('billing', 'CashHandover', {}),
    ('guests', 'DepositTransaction', {}),
    ('guests', 'BedTransfer', {}),
    ('messaging', 'MessageLog', {}),
    ('accounting', 'LedgerEntry', {}),
    ('accounting', 'RecurringExpense', {}),
    ('guests', 'Tenancy', {}),
    ('guests', 'GuestDocument', {}),
    ('guests', 'Guest', {}),
    ('properties', 'Bed', {}),
    ('properties', 'Room', {}),
    ('properties', 'Floor', {}),
    ('properties', 'Building', {}),
    ('properties', 'RoomType', {}),
    ('accounting', 'Category', {}),
    ('messaging', 'ReminderRule', {}),
    ('messaging', 'MessageTemplate', {}),
    ('billing', 'LateFeeRule', {}),
    ('billing', 'PrinterConfig', {}),
    ('accounts', 'StaffAssignment', {}),
    ('accounts', 'StaffPermission', {}),
    ('accounts', 'LoginHistory', {}),
    ('core', 'Branding', {}),
    ('core', 'FeatureFlag', {}),
    ('core', 'Subscription', {}),
    ('core', 'Sequence', {}),
    ('core', 'SystemNotice', {}),
    # Per-client statuses go; the platform templates (tenant=None) stay unless
    # --platform-statuses is passed.
    ('core', 'Status', {'tenant__isnull': False}),
    ('core', 'AuditLog', {}),
]


class Command(BaseCommand):
    help = ('Delete every client and all their data. Keeps the super admin and the '
            'platform default statuses unless told otherwise.')

    def add_arguments(self, parser):
        parser.add_argument('--yes', action='store_true',
                            help='Required. Without it nothing is deleted.')
        parser.add_argument('--no-backup', action='store_true',
                            help='Skip the automatic backup taken before deleting.')
        parser.add_argument('--all-users', action='store_true',
                            help='Also delete super admins, leaving no way to sign in.')
        parser.add_argument('--platform-statuses', action='store_true',
                            help='Also delete the platform default status templates.')

    def handle(self, *args, **options):
        from django.apps import apps

        from accounts.models import User

        if not options['yes']:
            raise CommandError(
                'This deletes every client and all their data. Re-run with --yes '
                'once you are sure. A backup is taken first unless --no-backup.'
            )

        if not options['no_backup']:
            from django.core.management import call_command

            self.stdout.write('Taking a backup first…')
            call_command('backup_db', keep=30, json=True)

        order = list(WIPE_ORDER)
        if options['platform_statuses']:
            order = [(a, m, {} if m == 'Status' else f) for a, m, f in order]

        removed = {}
        with transaction.atomic():
            for app_label, model_name, filters in order:
                model = apps.get_model(app_label, model_name)
                queryset = model.objects.filter(**filters) if filters else model.objects.all()
                count = queryset.count()
                if count:
                    queryset.delete()
                    removed[f'{app_label}.{model_name}'] = count

            users = User.objects.all()
            if not options['all_users']:
                users = users.exclude(role=User.ROLE_SUPER_ADMIN)
            user_count = users.count()
            if user_count:
                users.delete()
                removed['accounts.User'] = user_count

            tenant_model = apps.get_model('core', 'Tenant')
            tenant_count = tenant_model.objects.count()
            if tenant_count:
                tenant_model.objects.all().delete()
                removed['core.Tenant'] = tenant_count

        self.stdout.write(self.style.SUCCESS('\nDatabase cleared.'))
        if removed:
            width = max(len(name) for name in removed)
            for name, count in sorted(removed.items(), key=lambda row: -row[1]):
                self.stdout.write(f'  {count:>6}  {name.ljust(width)}  deleted')
        else:
            self.stdout.write('  nothing to delete — it was already empty')

        survivors = User.objects.all()
        self.stdout.write('')
        if survivors.exists():
            self.stdout.write(self.style.SUCCESS('Accounts kept:'))
            for user in survivors:
                self.stdout.write(f'  {user.username}  ({user.get_role_display()})')
        else:
            self.stdout.write(self.style.WARNING(
                'No accounts remain. Run "manage.py seed_platform --username <name> '
                '--password <pw>" before anyone can sign in.'
            ))

        status_model = apps.get_model('core', 'Status')
        kept = status_model.objects.filter(tenant__isnull=True).count()
        self.stdout.write(f'Platform default statuses kept: {kept}')
        if not kept:
            self.stdout.write(self.style.WARNING(
                'Run "manage.py seed_platform" to restore them, or new clients will '
                'start with no bed, tenancy or invoice statuses.'
            ))
        self.stdout.write(f'\nDone at {timezone.localtime():%d %b %Y %H:%M}.')
