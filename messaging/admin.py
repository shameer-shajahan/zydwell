from django.contrib import admin

from .models import MessageLog, MessageTemplate, ReminderRule


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'language', 'tenant', 'is_approved', 'is_active']
    list_filter = ['tenant', 'is_approved', 'is_active']


@admin.register(MessageLog)
class MessageLogAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'tenant', 'to_number', 'template_code', 'status']
    list_filter = ['status', 'tenant', 'template_code']
    search_fields = ['to_number', 'body']
    readonly_fields = ['provider_message_id']


admin.site.register([ReminderRule])
