from django.urls import path

from . import views

app_name = 'teams'

urlpatterns = [
    path('', views.team_list, name='team_list'),
    path('new/', views.team_create, name='team_create'),
    path('import/', views.team_import, name='team_import'),
    path('raise-invoices/', views.raise_invoices, name='raise_invoices'),
    path('<int:pk>/', views.team_detail, name='team_detail'),
    path('<int:pk>/edit/', views.team_edit, name='team_edit'),
]
