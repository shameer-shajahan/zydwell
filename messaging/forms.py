from django import forms
from django.utils.translation import gettext_lazy as _

from guests.models import Guest

from .models import MessageTemplate, ReminderRule


class TemplateForm(forms.ModelForm):
    class Meta:
        model = MessageTemplate
        fields = ['code', 'name', 'language', 'whatsapp_template_name', 'body', 'is_approved',
                  'is_active']
        widgets = {'body': forms.Textarea(attrs={'rows': 4})}


class ReminderRuleForm(forms.ModelForm):
    class Meta:
        model = ReminderRule
        fields = ['offset_days', 'template', 'is_active']

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['template'].queryset = MessageTemplate.objects.filter(
            tenant=tenant, is_active=True
        )


class ManualSendForm(forms.Form):
    guest = forms.ModelChoiceField(label=_('Guest'), queryset=Guest.objects.none())
    template = forms.ModelChoiceField(label=_('Template'),
                                      queryset=MessageTemplate.objects.none(), required=False)
    reason = forms.CharField(label=_('Reason / subject'), required=False, max_length=200,
                             help_text=_('Fills the {reason} placeholder in warning templates.'))
    body = forms.CharField(label=_('Message'), widget=forms.Textarea(attrs={'rows': 5}),
                           required=False,
                           help_text=_('Leave empty to use the rendered template text.'))

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['guest'].queryset = Guest.objects.filter(tenant=tenant)
        self.fields['template'].queryset = MessageTemplate.objects.filter(
            tenant=tenant, is_active=True
        )

    def clean(self):
        data = super().clean()
        if not data.get('template') and not data.get('body'):
            raise forms.ValidationError(_('Pick a template or write a message.'))
        return data


class BroadcastForm(forms.Form):
    building = forms.ModelChoiceField(label=_('Building'), queryset=None, required=False,
                                      help_text=_('Leave empty to reach every current guest.'))
    message = forms.CharField(label=_('Notice'), widget=forms.Textarea(attrs={'rows': 4}))

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        from properties.models import Building

        self.fields['building'].queryset = Building.objects.filter(
            tenant=tenant, is_archived=False
        )
