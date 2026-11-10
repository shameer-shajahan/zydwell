from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('staff/', views.staff_list, name='staff_list'),
    path('staff/new/', views.staff_form, name='staff_create'),
    path('staff/<int:pk>/edit/', views.staff_form, name='staff_edit'),
    path('staff/<int:pk>/access/', views.staff_permissions, name='staff_permissions'),
    path('staff/<int:pk>/toggle/', views.staff_toggle, name='staff_toggle'),
    path('assignments/<int:pk>/delete/', views.assignment_delete, name='assignment_delete'),
    path('login-history/', views.login_history, name='login_history'),
]
