from django import forms
from django.utils.translation import gettext_lazy as _

from properties.models import Apartment, Building, Floor, Room

from .models import KIND_EXPENSE, KIND_INCOME, Category, LedgerEntry, RecurringExpense


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['kind', 'name', 'code', 'description', 'sort_order', 'is_active']

    def clean_code(self):
        code = self.cleaned_data['code']
        kind = self.data.get('kind')
        clash = Category.objects.filter(
            tenant=self.instance.tenant_id, kind=kind, code=code
        ).exclude(pk=self.instance.pk)
        if self.instance.tenant_id and clash.exists():
            raise forms.ValidationError(_('That code is already used for this kind.'))
        return code


class LedgerEntryForm(forms.ModelForm):
    class Meta:
        model = LedgerEntry
        fields = ['kind', 'category', 'amount', 'entry_date', 'description', 'building',
                  'floor', 'apartment', 'room', 'payee', 'reference', 'attachment']
        widgets = {'entry_date': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, tenant=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = Category.objects.filter(tenant=tenant, is_active=True)
        buildings = Building.objects.filter(tenant=tenant, is_archived=False)
        floors = Floor.objects.filter(tenant=tenant, is_archived=False).select_related('building')
        apartments = Apartment.objects.filter(
            tenant=tenant, is_archived=False).select_related('floor')
        rooms = Room.objects.filter(
            tenant=tenant, is_archived=False).select_related('apartment')
        if user is not None and user.is_staff_member:
            buildings = user.scope_queryset(buildings, building_field='pk', floor_field='floors')
            floors = user.scope_queryset(floors, building_field='building', floor_field='pk')
            apartments = user.scope_queryset(
                apartments, building_field='floor__building', floor_field='floor',
                apartment_field='pk')
            rooms = user.scope_queryset(
                rooms, building_field='apartment__floor__building',
                floor_field='apartment__floor', apartment_field='apartment')
        self.fields['building'].queryset = buildings
        self.fields['floor'].queryset = floors
        self.fields['apartment'].queryset = apartments
        self.fields['room'].queryset = rooms
        for name in ('building', 'floor', 'apartment', 'room'):
            self.fields[name].required = False

    def clean(self):
        data = super().clean()
        kind, category = data.get('kind'), data.get('category')
        if kind and category and category.kind != kind:
            self.add_error('category', _('That category belongs to the other ledger.'))
        building = data.get('building')
        floor, apartment, room = data.get('floor'), data.get('apartment'), data.get('room')
        if room and apartment and room.apartment_id != apartment.id:
            self.add_error('room', _('That room is not in the selected apartment.'))
        if apartment and floor and apartment.floor_id != floor.id:
            self.add_error('apartment', _('That apartment is not on the selected floor.'))
        if floor and building and floor.building_id != building.id:
            self.add_error('floor', _('That floor is not in the selected building.'))
        if not (building or floor or apartment or room):
            self.add_error('building',
                           _('Tag the entry to a building, floor, apartment or room.'))
        return data


class RecurringExpenseForm(forms.ModelForm):
    class Meta:
        model = RecurringExpense
        fields = ['name', 'category', 'amount', 'day_of_month', 'building', 'floor', 'payee',
                  'is_active']

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = Category.objects.filter(
            tenant=tenant, kind=KIND_EXPENSE, is_active=True
        )
        self.fields['building'].queryset = Building.objects.filter(tenant=tenant, is_archived=False)
        self.fields['floor'].queryset = Floor.objects.filter(tenant=tenant, is_archived=False)
