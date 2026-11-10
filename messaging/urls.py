from django.urls import path

from . import views

app_name = 'messaging'

urlpatterns = [
    path('', views.log, name='log'),
    path('send/', views.send_manual, name='send_manual'),
    path('open/', views.quick_send, name='quick_send'),
    path('waiting/', views.waiting, name='waiting'),
    path('<int:pk>/open/', views.open_log, name='open_log'),
    path('<int:pk>/delete/', views.log_delete, name='log_delete'),
    path('broadcast/', views.broadcast, name='broadcast'),
    path('templates/', views.template_list, name='template_list'),
    path('templates/<int:pk>/edit/', views.template_edit, name='template_edit'),
    path('rules/save/', views.rule_save, name='rule_save'),
    path('rules/<int:pk>/delete/', views.rule_delete, name='rule_delete'),
]
