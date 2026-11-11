from django.urls import path

from . import views

app_name = 'platform'

urlpatterns = [
    path('', views.tenant_list, name='tenant_list'),
    path('clients/new/', views.tenant_form, name='tenant_create'),
    path('clients/<int:pk>/', views.tenant_detail, name='tenant_detail'),
    path('clients/<int:pk>/edit/', views.tenant_form, name='tenant_edit'),
    path('clients/<int:pk>/status/', views.tenant_status, name='tenant_status'),
    path('clients/<int:pk>/delete/', views.tenant_delete, name='tenant_delete'),
    path('clients/<int:pk>/keys/', views.tenant_integrations,
         name='tenant_integrations'),
    path('clients/<int:pk>/open/', views.switch_tenant, name='switch_tenant'),
    path('users/<int:pk>/password/', views.reset_admin_password,
         name='reset_admin_password'),
    path('users/<int:pk>/impersonate/', views.impersonate, name='impersonate'),
    path('stop-impersonation/', views.stop_impersonation, name='stop_impersonation'),
    path('audit/', views.global_audit, name='global_audit'),
    path('health/', views.system_health, name='system_health'),
    path('notices/save/', views.notice_save, name='notice_save'),
    path('notices/<int:pk>/toggle/', views.notice_toggle, name='notice_toggle'),
    path('statuses/', views.global_statuses, name='global_statuses'),
]
