from django.contrib import admin

from .models import LearnedPhrase


@admin.register(LearnedPhrase)
class LearnedPhraseAdmin(admin.ModelAdmin):
    list_display = ['text', 'intent', 'tenant', 'taught_by', 'created_at']
    list_filter = ['tenant', 'intent']
    search_fields = ['text']
