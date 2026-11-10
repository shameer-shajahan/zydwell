from django.contrib import admin

from .models import Bed, Building, Floor, Room, RoomType


class BedInline(admin.TabularInline):
    model = Bed
    extra = 0


@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    list_display = ['name', 'tenant', 'city', 'licence_number', 'is_archived']
    list_filter = ['tenant', 'is_archived']
    search_fields = ['name', 'licence_number']


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ['number', 'floor', 'room_type', 'capacity', 'rent_basis', 'monthly_rent']
    list_filter = ['tenant', 'rent_basis', 'room_type', 'is_archived']
    search_fields = ['number']
    inlines = [BedInline]


@admin.register(Bed)
class BedAdmin(admin.ModelAdmin):
    list_display = ['label', 'room', 'monthly_price', 'status', 'is_archived']
    list_filter = ['tenant', 'status', 'is_archived']


admin.site.register([Floor, RoomType])
