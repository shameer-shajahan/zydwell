from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import LoginHistory, StaffAssignment, StaffPermission, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ['username', 'get_full_name', 'role', 'tenant', 'is_active']
    list_filter = ['role', 'tenant', 'is_active']
    fieldsets = DjangoUserAdmin.fieldsets + (
        ('ZyDwell', {'fields': ('role', 'tenant', 'phone', 'job_title', 'language', 'avatar',
                                'two_factor_enabled', 'ip_allow_list',
                                'must_change_password')}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ('ZyDwell', {'fields': ('role', 'tenant')}),
    )


@admin.register(LoginHistory)
class LoginHistoryAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'username_tried', 'tenant', 'successful', 'ip_address']
    list_filter = ['successful', 'tenant']


admin.site.register([StaffPermission, StaffAssignment])
