"""Uploads stored in the database instead of on the server's disk.

Every upload (logos, guest photos, ID scans, bill photos) goes through
Django's default storage, so pointing ``STORAGES['default']`` here moves them
all without touching a model or form. Photos are shrunk on the way in so the
database does not fill up with 5 MB phone pictures.

A file saved before this existed is still read from MEDIA_ROOT if it is
there; ``manage.py move_media_to_db`` copies such files in for good.
"""
import io
import mimetypes
import posixpath
from urllib.parse import urljoin

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage
from django.utils.deconstruct import deconstructible
from django.utils.encoding import filepath_to_uri

#: Longest side, in pixels, a photo is reduced to. Passport and ID scans
#: stay easy to read at this size.
MAX_SIDE = 2000
SHRINKABLE = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP'}


def shrink(name, data):
    """``data`` made smaller if it is a large photo; unchanged otherwise."""
    fmt = SHRINKABLE.get(posixpath.splitext(name)[1].lower())
    if fmt is None:
        return data
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            if fmt == 'JPEG' and image.mode not in ('RGB', 'L'):
                image = image.convert('RGB')
            out = io.BytesIO()
            options = {'optimize': True}
            if fmt in ('JPEG', 'WEBP'):
                options['quality'] = 85
            image.save(out, fmt, **options)
    except Exception:  # not really an image, or one Pillow cannot read
        return data
    smaller = out.getvalue()
    return smaller if len(smaller) < len(data) else data


def _disk():
    return FileSystemStorage(location=settings.MEDIA_ROOT)


def _on_disk(name):
    try:
        return _disk().exists(name)
    except SuspiciousFileOperation:
        return False


@deconstructible
class DatabaseStorage(Storage):
    def _rows(self, name):
        from core.models import StoredFile

        return StoredFile.objects.filter(name=name)

    def _open(self, name, mode='rb'):
        content = self._rows(name).values_list('content', flat=True).first()
        if content is not None:
            return ContentFile(bytes(content), name=name)
        if _on_disk(name):
            return _disk().open(name, mode)
        raise FileNotFoundError(name)

    def _save(self, name, content):
        from core.models import StoredFile

        content.seek(0)
        data = shrink(name, content.read())
        StoredFile.objects.create(
            name=name, content=data, size=len(data),
            content_type=mimetypes.guess_type(name)[0] or '')
        return name

    def exists(self, name):
        return self._rows(name).exists() or _on_disk(name)

    def delete(self, name):
        self._rows(name).delete()

    def size(self, name):
        size = self._rows(name).values_list('size', flat=True).first()
        if size is not None:
            return size
        if _on_disk(name):
            return _disk().size(name)
        raise FileNotFoundError(name)

    def url(self, name):
        return urljoin(settings.MEDIA_URL, filepath_to_uri(name))

    def listdir(self, path):
        from core.models import StoredFile

        prefix = path.rstrip('/') + '/' if path else ''
        dirs, files = set(), []
        for name in StoredFile.objects.filter(name__startswith=prefix).values_list(
                'name', flat=True):
            rest = name[len(prefix):]
            if '/' in rest:
                dirs.add(rest.split('/', 1)[0])
            else:
                files.append(rest)
        return sorted(dirs), sorted(files)
