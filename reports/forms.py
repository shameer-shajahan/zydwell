from django import forms
from django.utils.translation import gettext_lazy as _

from core.models import Status, Tenant


class TenantSettingsForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = ['name', 'contact_person', 'contact_email', 'contact_phone', 'currency',
                  'date_format', 'tax_percent']
        help_texts = {
            'date_format': _('Django date format, for example "d M Y".'),
        }


class StatusForm(forms.ModelForm):
    class Meta:
        model = Status
        fields = ['kind', 'code', 'name', 'color', 'counts_as_available', 'counts_as_occupied',
                  'counts_as_settled', 'is_default', 'sort_order']
        widgets = {'color': forms.TextInput(attrs={'type': 'color'})}

    def clean(self):
        data = super().clean()
        if data.get('counts_as_available') and data.get('counts_as_occupied'):
            raise forms.ValidationError(
                _('A status cannot count as both available and occupied.')
            )
        return data
