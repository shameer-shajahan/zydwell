"""Seed the platform: status templates and the seeded-only super admin account."""
import os
import secrets

from django.core.management.base import BaseCommand

from accounts.models import User
from core.seed import seed_platform_status_templates


class Command(BaseCommand):
    help = 'Create the platform status templates and the super admin account.'

    def add_arguments(self, parser):
        parser.add_argument('--username', default=os.environ.get('ZYDWELL_SUPERADMIN', 'superadmin'))
        parser.add_argument('--email', default=os.environ.get('ZYDWELL_SUPERADMIN_EMAIL', ''))
        parser.add_argument('--password', default=os.environ.get('ZYDWELL_SUPERADMIN_PASSWORD', ''))
        parser.add_argument('--reset-password', action='store_true',
                            help='Set the password even if the account already exists.')

    def handle(self, *args, **options):
        seed_platform_status_templates()
        self.stdout.write(self.style.SUCCESS('Platform status templates ready.'))

        username = options['username']
        user = User.objects.filter(username=username).first()
        if user:
            if options['reset_password'] and options['password']:
                user.set_password(options['password'])
                user.save(update_fields=['password'])
                self.stdout.write(self.style.SUCCESS(f'Password reset for "{username}".'))
            else:
                self.stdout.write(f'Super admin "{username}" already exists.')
            return

        password = options['password'] or secrets.token_urlsafe(12)
        user = User.objects.create_superuser(
            username=username,
            email=options['email'],
            password=password,
            role=User.ROLE_SUPER_ADMIN,
            two_factor_enabled=True,
            first_name='Platform',
            last_name='Owner',
        )
        user.tenant = None
        user.save(update_fields=['tenant'])
        self.stdout.write(self.style.SUCCESS(f'Super admin created: {username}'))
        if not options['password']:
            self.stdout.write(self.style.WARNING(f'Generated password: {password}'))
            self.stdout.write('Change it after the first login.')
