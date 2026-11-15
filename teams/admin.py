from django.contrib import admin

from .models import TeamLet


@admin.register(TeamLet)
class TeamLetAdmin(admin.ModelAdmin):
    list_display = ['team_name', 'guest', 'apartment', 'members', 'monthly_rent', 'status']
    list_filter = ['tenant', 'status', 'building']
    search_fields = ['team_name', 'guest__full_name', 'guest__mobile']
