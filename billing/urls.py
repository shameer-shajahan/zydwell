from django.urls import path

from . import views

app_name = 'billing'

urlpatterns = [
    path('invoices/', views.invoice_list, name='invoice_list'),
    path('invoices/new/', views.invoice_create, name='invoice_create'),
    path('invoices/run/', views.run_billing, name='run_billing'),
    path('invoices/<int:pk>/', views.invoice_detail, name='invoice_detail'),
    path('invoices/<int:pk>/edit/', views.invoice_edit, name='invoice_edit'),
    path('invoices/<int:pk>/delete/', views.invoice_delete, name='invoice_delete'),
    path('invoices/<int:pk>/apply-credit/', views.apply_credit, name='apply_credit'),
    path('payments/', views.payment_list, name='payment_list'),
    path('payments/new/', views.payment_create, name='payment_create'),
    path('payments/<int:pk>/', views.payment_detail, name='payment_detail'),
    path('payments/<int:pk>/edit/', views.payment_edit, name='payment_edit'),
    path('payments/<int:pk>/delete/', views.payment_delete, name='payment_delete'),
    path('payments/<int:pk>/reverse/', views.payment_reverse, name='payment_reverse'),
    path('payments/<int:pk>/receipt/', views.receipt_view, name='receipt'),
    path('payments/<int:pk>/receipt/print/', views.receipt_print, name='receipt_print'),
    path('payments/<int:pk>/receipt/whatsapp/', views.receipt_whatsapp,
         name='receipt_whatsapp'),
    path('payments/<int:pk>/receipt/escpos/', views.receipt_escpos, name='receipt_escpos'),
    path('overdue/', views.overdue, name='overdue'),
    path('collection/', views.daily_collection, name='daily_collection'),
    path('handovers/<int:pk>/confirm/', views.handover_confirm, name='handover_confirm'),
    path('printers/', views.printer_list, name='printer_list'),
    path('printers/new/', views.printer_form, name='printer_create'),
    path('printers/<int:pk>/edit/', views.printer_form, name='printer_edit'),
    path('printers/<int:pk>/test/', views.printer_test, name='printer_test'),
    path('approvals/', views.approvals, name='approvals'),
    path('approvals/payment/<int:pk>/<str:verdict>/', views.payment_decide,
         name='payment_decide'),
    path('approvals/deposit/<int:pk>/<str:verdict>/', views.deposit_decide,
         name='deposit_decide'),
    path('modes/', views.payment_mode_list, name='payment_mode_list'),
    path('modes/<int:pk>/edit/', views.payment_mode_edit, name='payment_mode_edit'),
    path('modes/<int:pk>/delete/', views.payment_mode_delete, name='payment_mode_delete'),
    path('late-fee/', views.late_fee_rule, name='late_fee_rule'),
]
