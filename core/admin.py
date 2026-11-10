from django.contrib import admin

from .models import (
    AuditLog, Branding, FeatureFlag, Sequence, Status, Subscription, SystemNotice, Tenant,
)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'status', 'currency', 'max_beds', 'created_at']
    list_filter = ['status', 'currency']
    search_fields = ['name', 'slug', 'contact_person']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Status)
class StatusAdmin(admin.ModelAdmin):
    list_display = ['name', 'kind', 'code', 'tenant', 'counts_as_available',
                    'counts_as_occupied', 'counts_as_settled', 'is_default']
    list_filter = ['kind', 'tenant', 'is_default']
    search_fields = ['name', 'code']


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'tenant', 'actor_label', 'action', 'model_name', 'summary']
    list_filter = ['action', 'tenant', 'model_name']
    search_fields = ['summary', 'object_label', 'actor_label']
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register([Subscription, Branding, FeatureFlag, SystemNotice, Sequence])
admin.site.site_header = 'ZyDwell platform administration'
admin.site.site_title = 'ZyDwell'
admin.site.index_title = 'Data administration'
