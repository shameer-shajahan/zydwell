"""python manage.py import_team_lets team_lets.csv [--dry-run] [--tenant NAME]"""
from django.core.management.base import BaseCommand, CommandError

from core.models import Tenant
from teams import importer


class Command(BaseCommand):
    help = 'Import whole-apartment team lets from a CSV team sheet.'

    def add_arguments(self, parser):
        parser.add_argument('csv_file')
        parser.add_argument('--dry-run', action='store_true',
                            help='Check and preview every row; save nothing.')
        parser.add_argument('--tenant', default='',
                            help='Client account name or slug. Needed when there is more '
                                 'than one.')

    def handle(self, *args, csv_file, dry_run, tenant, **options):
        account = self._tenant(tenant)
        try:
            with open(csv_file, encoding='utf-8-sig') as handle:
                text = handle.read()
        except OSError as exc:
            raise CommandError(f'Cannot read {csv_file}: {exc}') from exc

        plan = importer.read(text, account)
        for message in plan.file_errors:
            self.stderr.write(self.style.ERROR(message))
        self._preview(plan)
        totals = plan.totals
        self.stdout.write(
            f'\n{account.name}: {plan.count_create} create, {plan.count_update} update, '
            f'{plan.count_unchanged} unchanged, {plan.count_error} error. '
            f'File totals: {totals["members"]} members, rent {totals["rent"]:,.2f}, '
            f'deposits {totals["deposit"]:,.2f}.')
        if not plan.ok:
            raise CommandError('Nothing was saved — fix the errors above and run again.')
        if dry_run:
            self.stdout.write('Dry run: nothing saved. Run again without --dry-run to save.')
            return
        result = importer.apply(plan, account)
        self.stdout.write(self.style.SUCCESS(
            f'Saved: {result["created"]} new team lets, {result["updated"]} updated.'))

    def _tenant(self, name):
        accounts = Tenant.objects.all()
        if name:
            match = list(accounts.filter(name__iexact=name)) or \
                list(accounts.filter(slug__iexact=name))
            if len(match) != 1:
                raise CommandError(f'No single client account named “{name}”.')
            return match[0]
        if accounts.count() != 1:
            names = ', '.join(accounts.values_list('name', flat=True)[:10])
            raise CommandError(f'Pick the client account with --tenant (one of: {names}).')
        return accounts.get()

    def _preview(self, plan):
        for row in plan.rows:
            flat = f'{row.raw.get("building", "")} / {row.raw.get("apartment", "")}'
            self.stdout.write(f'{row.line:>4}  {row.action:<9} {row.label[:34]:<34} {flat}')
            for message in row.errors:
                self.stdout.write(self.style.ERROR(f'        error: {message}'))
            for name, old, new in row.changes:
                self.stdout.write(f'        {name}: {old} → {new}')
            for message in row.warnings:
                self.stdout.write(self.style.WARNING(f'        {message}'))
