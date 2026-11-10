from django.contrib import admin

from .models import BedTransfer, DepositTransaction, Guest, GuestDocument, Tenancy


class DocumentInline(admin.TabularInline):
    model = GuestDocument
    extra = 0


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'mobile', 'tenant', 'nationality', 'employer']
    list_filter = ['tenant', 'nationality', 'is_blacklisted']
    search_fields = ['full_name', 'mobile', 'email']
    inlines = [DocumentInline]


@admin.register(Tenancy)
class TenancyAdmin(admin.ModelAdmin):
    list_display = ['guest', 'building', 'room', 'bed', 'check_in_date', 'monthly_rent', 'status']
    list_filter = ['tenant', 'status', 'building']
    search_fields = ['guest__full_name']
    date_hierarchy = 'check_in_date'


@admin.register(GuestDocument)
class GuestDocumentAdmin(admin.ModelAdmin):
    list_display = ['guest', 'doc_type', 'number', 'expiry_date']
    list_filter = ['doc_type', 'tenant']


admin.site.register([DepositTransaction, BedTransfer])
