from django.urls import path

from . import views

app_name = 'guests'

urlpatterns = [
    path('', views.guest_list, name='guest_list'),
    path('new/', views.guest_form, name='guest_create'),
    path('<int:pk>/', views.guest_detail, name='guest_detail'),
    path('<int:pk>/edit/', views.guest_form, name='guest_edit'),
    path('<int:pk>/statement/', views.guest_statement, name='guest_statement'),
    path('<int:pk>/delete/', views.guest_delete, name='guest_delete'),
    path('<int:pk>/share-link/', views.share_profile_link, name='share_profile_link'),
    path('links/<int:pk>/send/', views.send_profile_link, name='send_profile_link'),
    path('links/<int:pk>/revoke/', views.revoke_profile_link, name='revoke_profile_link'),
    path('documents/<int:pk>/delete/', views.document_delete, name='document_delete'),
    path('documents/expiring/', views.expiring_documents, name='expiring_documents'),
    path('tenancies/', views.tenancy_list, name='tenancy_list'),
    path('tenancies/check-in/', views.check_in, name='check_in'),
    path('tenancies/<int:pk>/', views.tenancy_detail, name='tenancy_detail'),
    path('tenancies/<int:pk>/edit/', views.tenancy_edit, name='tenancy_edit'),
    path('tenancies/<int:pk>/check-out/', views.check_out, name='check_out'),
    path('tenancies/<int:pk>/transfer/', views.transfer, name='transfer'),
    path('tenancies/<int:pk>/notice/', views.give_notice, name='give_notice'),
    path('tenancies/<int:pk>/deposit/', views.add_deposit, name='add_deposit'),
    path('deposits/<int:pk>/receipt/', views.deposit_receipt, name='deposit_receipt'),
    path('deposits/<int:pk>/receipt/print/', views.deposit_receipt_print,
         name='deposit_receipt_print'),
    path('deposits/<int:pk>/receipt/whatsapp/', views.deposit_receipt_whatsapp,
         name='deposit_receipt_whatsapp'),
]
