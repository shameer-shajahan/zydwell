from django.contrib import admin

from .models import CashHandover, Invoice, InvoiceLine, LateFeeRule, Payment, PrinterConfig


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ['number', 'tenancy', 'period_start', 'due_date', 'total', 'paid_amount',
                    'status']
    list_filter = ['tenant', 'status']
    search_fields = ['number', 'tenancy__guest__full_name']
    date_hierarchy = 'issue_date'
    inlines = [InvoiceLineInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['receipt_number', 'paid_on', 'tenancy', 'amount', 'mode',
                    'collected_by_name', 'is_reversed']
    list_filter = ['tenant', 'mode', 'is_reversed']
    search_fields = ['receipt_number', 'tenancy__guest__full_name']
    date_hierarchy = 'paid_on'
    readonly_fields = ['receipt_number']


admin.site.register([LateFeeRule, PrinterConfig, CashHandover])
