"""Serving uploaded files in production.

``django.conf.urls.static.static()`` only works while DEBUG is on, so with
DEBUG off every photo, logo and scan answered 404. Uploads are not static
files either — ``collectstatic`` never sees them — so Django serves them
here, from the database (``core.storage``).

Most uploads are private: passport and Emirates ID scans, guest photos, bill
photos. Those need a signed-in user of the account the file belongs to, with
the right to see that part of the app. Only logos are public, because the
sign-in and error pages show them to people who are not signed in.
"""
import mimetypes
import posixpath

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404

from core.constants import MODULE_ACCOUNTING, MODULE_GUESTS

#: Folders anyone may read: brand and building logos.
PUBLIC = ('branding/', 'buildings/')

#: Opened in the browser tab. Anything else (an .html or .svg someone
#: uploaded as a "scan") downloads instead, so it can never run as a page of
#: this site.
INLINE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/heic',
                'image/heif', 'image/bmp', 'application/pdf'}


def _owner(path):
    """``(tenant_id, module)`` of the record holding ``path``; ``None`` if none does."""
    from accounting.models import LedgerEntry
    from accounts.models import User
    from guests.models import Guest, GuestDocument

    if path.startswith('documents/'):
        row = GuestDocument.objects.filter(scan=path).values('tenant_id').first()
        return row and (row['tenant_id'], MODULE_GUESTS)
    if path.startswith('guests/'):
        row = Guest.objects.filter(photo=path).values('tenant_id').first()
        return row and (row['tenant_id'], MODULE_GUESTS)
    if path.startswith('ledger/'):
        row = LedgerEntry.objects.filter(attachment=path).values('tenant_id').first()
        return row and (row['tenant_id'], MODULE_ACCOUNTING)
    if path.startswith('avatars/'):
        row = User.objects.filter(avatar=path).values('tenant_id').first()
        return row and (row['tenant_id'], None)
    return None


def _may_see(request, path):
    user = request.user
    if user.is_super_admin:
        return True
    owner = _owner(path)
    if not owner:
        return False
    tenant_id, module = owner
    if tenant_id is None:
        # The platform super admin's own picture: fine for anyone signed in.
        return module is None
    tenant = getattr(request, 'tenant', None)
    if tenant is None or tenant.pk != tenant_id:
        return False
    return module is None or user.can(module, 'view')


def media(request, path):
    path = posixpath.normpath(path).lstrip('/')
    if path.startswith('..'):
        raise Http404
    public = path.startswith(PUBLIC)
    if not public:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        # Someone else's file looks exactly like a missing one.
        if not _may_see(request, path):
            raise Http404
    try:
        handle = default_storage.open(path)
    except (FileNotFoundError, SuspiciousFileOperation):
        raise Http404
    kind = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    response = FileResponse(handle, content_type=kind)
    if kind not in INLINE_TYPES:
        response['Content-Disposition'] = 'attachment; filename="%s"' % (
            posixpath.basename(path).replace('"', ''))
        response['Content-Security-Policy'] = 'sandbox'
    response['Cache-Control'] = ('public, max-age=86400' if public
                                 else 'private, max-age=3600')
    return response
