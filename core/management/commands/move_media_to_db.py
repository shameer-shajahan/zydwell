"""Copy uploads still on the server's disk (MEDIA_ROOT) into the database.

Safe to run more than once: a file already in the database is left alone.
Run it on any machine that still has the files, pointed at the database
they belong to. Disk files are kept unless --delete is given.
"""
import mimetypes
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import StoredFile
from core.storage import shrink


class Command(BaseCommand):
    help = 'Copy uploaded files from MEDIA_ROOT into the database.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='List, save nothing.')
        parser.add_argument('--delete', action='store_true',
                            help='Remove each disk file once it is in the database.')

    def handle(self, *args, dry_run=False, delete=False, **options):
        root = Path(settings.MEDIA_ROOT)
        if not root.is_dir():
            self.stdout.write(f'No folder at {root}; nothing to move.')
            return
        have = set(StoredFile.objects.values_list('name', flat=True))
        moved = skipped = 0
        before = after = 0
        for path in sorted(p for p in root.rglob('*') if p.is_file()):
            name = path.relative_to(root).as_posix()
            if name.startswith('.') or '/.' in name:
                continue
            if name in have:
                skipped += 1
                continue
            data = path.read_bytes()
            small = shrink(name, data)
            before, after = before + len(data), after + len(small)
            self.stdout.write(f'  {name}  {len(data) // 1024} KB → {len(small) // 1024} KB')
            if not dry_run:
                StoredFile.objects.create(name=name, content=small, size=len(small),
                                          content_type=mimetypes.guess_type(name)[0] or '')
                if delete:
                    path.unlink()
            moved += 1
        verb = 'Would move' if dry_run else 'Moved'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {moved} file(s), {before // 1024} KB → {after // 1024} KB. '
            f'{skipped} already in the database.'))
