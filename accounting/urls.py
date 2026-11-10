from django.urls import path

from . import views

app_name = 'accounting'

urlpatterns = [
    path('', views.ledger, name='ledger'),
    path('new/', views.entry_form, name='entry_create'),
    path('<int:pk>/edit/', views.entry_form, name='entry_edit'),
    path('<int:pk>/delete/', views.entry_delete, name='entry_delete'),
    path('categories/', views.category_list, name='category_list'),
    path('categories/<int:pk>/toggle/', views.category_toggle, name='category_toggle'),
    path('recurring/', views.recurring_list, name='recurring_list'),
    path('recurring/<int:pk>/delete/', views.recurring_delete, name='recurring_delete'),
    path('recurring/<int:pk>/run/', views.recurring_run, name='recurring_run'),
]
