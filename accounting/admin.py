from django.contrib import admin

from .models import Category, LedgerEntry, RecurringExpense


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'kind', 'code', 'tenant', 'is_active', 'is_system']
    list_filter = ['kind', 'tenant', 'is_active']


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ['entry_date', 'kind', 'category', 'amount', 'building', 'room',
                    'source_type']
    list_filter = ['kind', 'tenant', 'category', 'source_type']
    date_hierarchy = 'entry_date'


admin.site.register([RecurringExpense])
