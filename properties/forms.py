from django import forms
from django.utils.translation import gettext_lazy as _

from core.models import Status

from .models import Apartment, Bed, Building, Floor, Room, RoomType


class BuildingForm(forms.ModelForm):
    class Meta:
        model = Building
        fields = ['name', 'code', 'address', 'city', 'owner_name', 'licence_number',
                  'licence_expiry', 'contact_phone', 'contact_email', 'logo', 'receipt_footer']
        widgets = {
            'licence_expiry': forms.DateInput(attrs={'type': 'date'}),
            'address': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant

    def clean(self):
        data = super().clean()
        if self.instance.pk is None and self.tenant is not None:
            live = Building.objects.filter(tenant=self.tenant, is_archived=False).count()
            if live + 1 > self.tenant.max_buildings:
                raise forms.ValidationError(_(
                    'Your plan allows %(n)d buildings.'
                ) % {'n': self.tenant.max_buildings})
        return data


class FloorForm(forms.ModelForm):
    class Meta:
        model = Floor
        fields = ['building', 'name', 'sort_order']

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['building'].queryset = Building.objects.filter(tenant=tenant, is_archived=False)


class ApartmentForm(forms.ModelForm):
    class Meta:
        model = Apartment
        fields = ['floor', 'name', 'notes', 'sort_order']

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['floor'].queryset = Floor.objects.filter(
            tenant=tenant, is_archived=False,
            building__is_archived=False).select_related('building')


class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ['apartment', 'number', 'room_type', 'capacity', 'rent_basis',
                  'monthly_rent', 'has_ac', 'has_attached_bath', 'notes']
        widgets = {'notes': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        self.fields['apartment'].queryset = Apartment.objects.filter(
            tenant=tenant, is_archived=False, floor__is_archived=False,
            floor__building__is_archived=False
        ).select_related('floor', 'floor__building')
        self.fields['room_type'].queryset = RoomType.objects.filter(tenant=tenant)

    def clean(self):
        data = super().clean()
        if self.instance.pk is None and self.tenant is not None:
            live = Room.objects.filter(tenant=self.tenant).count()
            if live + 1 > self.tenant.max_rooms:
                raise forms.ValidationError(_('Your plan allows %(n)d rooms.') % {
                    'n': self.tenant.max_rooms})
        basis = data.get('rent_basis')
        if basis in (Room.RENT_PER_ROOM, Room.RENT_PER_FLAT) and not data.get('monthly_rent'):
            self.add_error('monthly_rent', _('Set the rent for the whole unit.'))
        return data


class BedForm(forms.ModelForm):
    class Meta:
        model = Bed
        fields = ['room', 'label', 'monthly_price', 'status', 'notes']

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        self.fields['room'].queryset = Room.objects.filter(
            tenant=tenant, is_archived=False, apartment__is_archived=False,
            apartment__floor__is_archived=False,
            apartment__floor__building__is_archived=False
        ).select_related('apartment', 'apartment__floor', 'apartment__floor__building')
        self.fields['status'].queryset = Status.objects.filter(
            tenant=tenant, kind=Status.KIND_BED
        )

    def clean(self):
        data = super().clean()
        if self.instance.pk is None and self.tenant is not None:
            live = Bed.objects.filter(tenant=self.tenant).count()
            if live + 1 > self.tenant.max_beds:
                raise forms.ValidationError(_('Your plan allows %(n)d beds.') % {
                    'n': self.tenant.max_beds})
        return data


class BulkBedForm(forms.Form):
    """Create several beds at once — A, B, C… up to the room capacity."""

    count = forms.IntegerField(label=_('How many beds'), min_value=1, max_value=20, initial=1)
    monthly_price = forms.DecimalField(label=_('Monthly price each'), max_digits=10, decimal_places=2)
    start_letter = forms.CharField(label=_('Start label'), max_length=2, initial='A')

    def __init__(self, *args, room=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.room = room

    def clean_count(self):
        count = self.cleaned_data['count']
        free = self.room.capacity - self.room.active_bed_count()
        if count > free:
            raise forms.ValidationError(_('Only %(n)d more bed(s) fit in this room.') % {'n': free})
        return count


class RoomTypeForm(forms.ModelForm):
    class Meta:
        model = RoomType
        fields = ['name', 'code', 'description', 'sort_order']
