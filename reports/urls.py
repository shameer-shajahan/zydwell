from django.urls import path

from . import views

app_name = 'reports'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('reports/', views.report_index, name='report_index'),
    path('reports/rent-roll/', views.rent_roll, name='rent_roll'),
    path('reports/collection/', views.collection_report, name='collection'),
    path('reports/expense/', views.expense_report, name='expense'),
    path('reports/profit/', views.profit_report, name='profit'),
    path('reports/deposits/', views.deposit_report, name='deposits'),
    path('reports/occupancy/', views.occupancy_report, name='occupancy'),
    path('export/rent-roll/', views.export_rent_roll, name='export_rent_roll'),
    path('export/collection/', views.export_collection, name='export_collection'),
    path('export/ledger/', views.export_ledger, name='export_ledger'),
    path('export/deposits/', views.export_deposits, name='export_deposits'),
    path('export/everything/', views.export_everything, name='export_everything'),
    path('settings/', views.settings_home, name='settings_home'),
    path('settings/statuses/', views.status_list, name='status_list'),
    path('settings/statuses/<int:pk>/edit/', views.status_edit, name='status_edit'),
    path('settings/statuses/<int:pk>/delete/', views.status_delete, name='status_delete'),
    path('settings/audit-log/', views.audit_log, name='audit_log'),
]
