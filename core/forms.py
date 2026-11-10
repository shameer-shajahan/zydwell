from django import forms
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from .models import (
    Branding, FeatureFlag, IntegrationSettings, Status, Subscription, SystemNotice,
    Tenant,
)


class TenantForm(forms.ModelForm):
    admin_username = forms.CharField(label=_('Admin username'), required=False)
    admin_email = forms.EmailField(label=_('Admin email'), required=False)
    admin_password = forms.CharField(label=_('Admin password'), required=False,
                                     widget=forms.PasswordInput)

    class Meta:
        model = Tenant
        fields = ['name', 'slug', 'contact_person', 'contact_email', 'contact_phone', 'status',
                  'currency', 'date_format', 'tax_percent', 'max_buildings', 'max_rooms',
                  'max_beds', 'max_staff_seats', 'notes']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['slug'].required = False
        if self.instance.pk:
            for name in ('admin_username', 'admin_email', 'admin_password'):
                self.fields[name].widget = forms.HiddenInput()
                self.fields[name].label = ''

    def clean_slug(self):
        slug = self.cleaned_data.get('slug') or slugify(self.data.get('name', ''))
        if Tenant.objects.filter(slug=slug).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(_('That slug is taken.'))
        return slug

    def clean(self):
        data = super().clean()
        if self.instance.pk is None:
            if not data.get('admin_username'):
                self.add_error('admin_username', _('Create the owner account too.'))
            if not data.get('admin_password'):
                self.add_error('admin_password', _('Set a first password for the owner.'))
            from accounts.models import User

            if data.get('admin_username') and User.objects.filter(
                username__iexact=data['admin_username']
            ).exists():
                self.add_error('admin_username', _('That username is taken.'))
        return data


class SubscriptionForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = ['plan', 'price', 'billing_cycle', 'starts_on', 'expires_on', 'grace_days',
                  'auto_lock', 'last_paid_on']
        widgets = {
            'starts_on': forms.DateInput(attrs={'type': 'date'}),
            'expires_on': forms.DateInput(attrs={'type': 'date'}),
            'last_paid_on': forms.DateInput(attrs={'type': 'date'}),
        }


class BrandingForm(forms.ModelForm):
    class Meta:
        model = Branding
        fields = ['display_name', 'logo', 'primary_color', 'accent_color', 'login_tagline',
                  'footer_note']
        widgets = {
            'primary_color': forms.TextInput(attrs={'type': 'color'}),
            'accent_color': forms.TextInput(attrs={'type': 'color'}),
        }


class SystemNoticeForm(forms.ModelForm):
    class Meta:
        model = SystemNotice
        fields = ['title', 'body', 'level', 'tenant', 'is_active']
        widgets = {'body': forms.Textarea(attrs={'rows': 2})}


class GlobalStatusForm(forms.ModelForm):
    """Edits the platform-level status templates pushed to new clients."""

    class Meta:
        model = Status
        fields = ['kind', 'code', 'name', 'color', 'counts_as_available', 'counts_as_occupied',
                  'counts_as_settled', 'is_default', 'sort_order']
        widgets = {'color': forms.TextInput(attrs={'type': 'color'})}


class IntegrationSettingsForm(forms.ModelForm):
    """Secrets are never sent back to the browser.

    Each secret renders empty with its last four characters in the help text;
    leaving it blank keeps whatever is stored, so nobody can read a token off a
    screen or out of a page source.
    """

    class Meta:
        model = IntegrationSettings
        fields = [
            'whatsapp_enabled', 'whatsapp_api_url', 'whatsapp_phone_number_id',
            'whatsapp_business_id', 'whatsapp_access_token',
            'email_enabled', 'email_host', 'email_port', 'email_use_tls',
            'email_username', 'email_password', 'email_from',
            'sms_enabled', 'sms_provider', 'sms_api_key', 'sms_sender_id',
            'payment_provider', 'payment_public_key', 'payment_secret_key',
        ]
        widgets = {'whatsapp_access_token': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in IntegrationSettings.SECRET_FIELDS:
            field = self.fields[name]
            field.required = False
            self.initial[name] = ''
            stored = self.instance.tail(name) if self.instance.pk else ''
            field.help_text = (
                _('Stored: %(tail)s — leave blank to keep it, or paste a new one.')
                % {'tail': stored} if stored
                else _('Nothing stored yet.')
            )
            if not isinstance(field.widget, forms.Textarea):
                field.widget = forms.PasswordInput(render_value=False,
                                                   attrs={'autocomplete': 'new-password'})

    def clean(self):
        data = super().clean()
        for name in IntegrationSettings.SECRET_FIELDS:
            if not (data.get(name) or '').strip():
                data[name] = getattr(self.instance, name, '')
        if data.get('whatsapp_enabled') and not (
                data.get('whatsapp_phone_number_id') and data.get('whatsapp_access_token')):
            self.add_error('whatsapp_enabled', _(
                'Automatic sending needs both a phone number ID and an access token.'))
        if data.get('email_enabled') and not (data.get('email_host')
                                              and data.get('email_from')):
            self.add_error('email_enabled', _('Email needs a host and a from address.'))
        return data

    def sections(self):
        def fields(*names):
            return [self[name] for name in names]

        return [
            (_('WhatsApp Business API'),
             _('With this on, reminders and receipts send by themselves. Templates must '
               'be approved by Meta first.'),
             fields('whatsapp_enabled', 'whatsapp_api_url', 'whatsapp_phone_number_id',
                    'whatsapp_business_id', 'whatsapp_access_token')),
            (_('Email'), _('Used for statements and anything a guest asks to be emailed.'),
             fields('email_enabled', 'email_host', 'email_port', 'email_use_tls',
                    'email_username', 'email_password', 'email_from')),
            (_('SMS gateway'), _('A fallback when a guest has no WhatsApp.'),
             fields('sms_enabled', 'sms_provider', 'sms_api_key', 'sms_sender_id')),
            (_('Payment gateway'), _('For collecting rent through a payment link.'),
             fields('payment_provider', 'payment_public_key', 'payment_secret_key')),
        ]


class AdminPasswordForm(forms.Form):
    """Set an owner's password without knowing the old one.

    The platform owner is resetting it precisely because nobody knows it, so
    asking for the current one would defeat the purpose. Instead the account is
    flagged to change it at next sign-in, so the platform owner does not keep a
    working password for someone else's account.
    """

    password = forms.CharField(
        label=_('New password'), required=False, widget=forms.PasswordInput,
        help_text=_('Leave blank to have one generated.'),
    )
    confirm = forms.CharField(
        label=_('Repeat the password'), required=False, widget=forms.PasswordInput)

    def __init__(self, *args, target=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target = target

    def clean(self):
        from django.contrib.auth.password_validation import validate_password

        data = super().clean()
        password = (data.get('password') or '').strip()
        if not password:
            return data                      # one will be generated
        if password != (data.get('confirm') or '').strip():
            self.add_error('confirm', _('The two passwords do not match.'))
            return data
        validate_password(password, self.target)
        return data

    def resolve(self):
        """The password to set — theirs if they typed one, otherwise a fresh one."""
        import secrets

        typed = (self.cleaned_data.get('password') or '').strip()
        return (typed, False) if typed else (secrets.token_urlsafe(9), True)
