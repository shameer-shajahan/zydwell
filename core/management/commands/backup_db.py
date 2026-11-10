"""Scheduled database backup. See docs/RUNBOOK.md for the restore procedure."""
import gzip
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Write a timestamped database backup into the backup directory.'

    def add_arguments(self, parser):
        parser.add_argument('--keep', type=int, default=14, help='How many backups to retain.')
        parser.add_argument('--json', action='store_true',
                            help='Also write a portable JSON dump of every model.')

    def handle(self, *args, **options):
        target = Path(settings.ZYDWELL['BACKUP_DIR'])
        target.mkdir(parents=True, exist_ok=True)
        stamp = timezone.localtime().strftime('%Y%m%d-%H%M%S')
        engine = settings.DATABASES['default']['ENGINE']

        if 'sqlite' in engine:
            source = Path(settings.DATABASES['default']['NAME'])
            out = target / f'zydwell-{stamp}.sqlite3.gz'
            with open(source, 'rb') as src, gzip.open(out, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        else:
            db = settings.DATABASES['default']
            out = target / f'zydwell-{stamp}.sql.gz'
            dump = subprocess.run(
                ['pg_dump', '-h', db['HOST'], '-p', str(db['PORT']), '-U', db['USER'], db['NAME']],
                capture_output=True, check=True,
                env={'PGPASSWORD': db['PASSWORD'], 'PATH': '/usr/bin:/bin:/usr/local/bin'},
            )
            with gzip.open(out, 'wb') as dst:
                dst.write(dump.stdout)

        self.stdout.write(self.style.SUCCESS(f'Backup written: {out}'))

        if options['json']:
            json_out = target / f'zydwell-{stamp}.json'
            with open(json_out, 'w', encoding='utf-8') as handle:
                call_command('dumpdata', '--natural-foreign', '--natural-primary',
                             '-e', 'contenttypes', '-e', 'auth.Permission',
                             '--indent', '2', stdout=handle)
            self.stdout.write(self.style.SUCCESS(f'JSON dump written: {json_out}'))

        backups = sorted(target.glob('zydwell-*'), reverse=True)
        for stale in backups[options['keep']:]:
            stale.unlink()
            self.stdout.write(f'Pruned {stale.name}')
