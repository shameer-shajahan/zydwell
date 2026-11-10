from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from billing import views as billing_views
from guests import views as guest_views

urlpatterns = [
    path('django-admin/', admin.site.urls),
    path('i18n/', include('django.conf.urls.i18n')),
    path('', include('reports.urls', namespace='reports')),
    path('accounts/', include('accounts.urls', namespace='accounts')),
    # Public, unauthenticated: a guest completing their own profile.
    path('g/<str:token>/', guest_views.guest_portal, name='guest_profile'),
    path('g/<str:token>/details/', guest_views.guest_portal_profile,
         name='guest_portal_profile'),
    path('g/<str:token>/receipts/', guest_views.guest_portal_receipts,
         name='guest_portal_receipts'),
    path('g/<str:token>/receipts/<str:kind>/<int:pk>/', guest_views.guest_portal_receipt,
         name='guest_portal_receipt'),
    # Public, unauthenticated: a guest opening their own receipt as a PDF.
    path('r/<str:token>/', billing_views.public_receipt, name='public_receipt'),
    path('d/<str:token>/', guest_views.public_deposit_receipt,
         name='public_deposit_receipt'),
    path('platform/', include('core.urls', namespace='platform')),
    path('properties/', include('properties.urls', namespace='properties')),
    path('guests/', include('guests.urls', namespace='guests')),
    path('billing/', include('billing.urls', namespace='billing')),
    path('accounting/', include('accounting.urls', namespace='accounting')),
    path('messaging/', include('messaging.urls', namespace='messaging')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    # Error pages only render when DEBUG is off, which makes them awkward to
    # work on. These previews exist in development only.
    from django.shortcuts import render

    def _preview(request, code):
        return render(request, f'{code}.html', status=int(code.split('_')[0]))

    urlpatterns += [
        path('__error__/<str:code>/', _preview, name='error_preview'),
    ]
