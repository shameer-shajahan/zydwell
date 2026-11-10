from django.urls import path

from . import views

app_name = 'properties'

urlpatterns = [
    path('', views.building_list, name='building_list'),
    path('board/', views.bed_board, name='bed_board'),
    path('new/', views.building_form, name='building_create'),
    path('<int:pk>/', views.building_detail, name='building_detail'),
    path('<int:pk>/edit/', views.building_form, name='building_edit'),
    path('<int:pk>/delete/', views.building_delete, name='building_delete'),
    path('floors/new/', views.floor_form, name='floor_create'),
    path('floors/<int:pk>/edit/', views.floor_form, name='floor_edit'),
    path('floors/<int:pk>/delete/', views.floor_delete, name='floor_delete'),
    path('apartments/new/', views.apartment_form, name='apartment_create'),
    path('apartments/<int:pk>/edit/', views.apartment_form, name='apartment_edit'),
    path('apartments/<int:pk>/delete/', views.apartment_delete, name='apartment_delete'),
    path('rooms/new/', views.room_form, name='room_create'),
    path('rooms/<int:pk>/', views.room_detail, name='room_detail'),
    path('rooms/<int:pk>/edit/', views.room_form, name='room_edit'),
    path('rooms/<int:pk>/delete/', views.room_delete, name='room_delete'),
    path('room-types/', views.room_type_list, name='room_type_list'),
    path('beds/new/', views.bed_form, name='bed_create'),
    path('beds/<int:pk>/edit/', views.bed_form, name='bed_edit'),
    path('beds/<int:pk>/status/', views.bed_status, name='bed_status'),
    path('beds/<int:pk>/delete/', views.bed_delete, name='bed_delete'),
]
