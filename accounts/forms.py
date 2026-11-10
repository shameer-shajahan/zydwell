from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.utils.translation import gettext_lazy as _

from core.constants import ADMIN_ONLY_MODULES, PERMISSION_MODULES
from properties.models import Apartment, Building, Floor

from .models import StaffAssignment, StaffPermission, User

STAFF_MODULES = [(m, label) for m, label in PERMISSION_MODULES if m not in ADMIN_ONLY_MODULES]


class LoginForm(AuthenticationForm):
    error_messages = {
        # Deliberately the same wording whether the username is unknown or the
        # password is wrong — naming which one is off would let anyone probe for
        # valid staff usernames.
        'invalid_login': _('That username and password do not match. Check for capital '
                           'letters and try again.'),
        'inactive': _('This account has been switched off. Ask your building owner to '
                      'turn it back on.'),
    }

    username = forms.CharField(
        label=_('Username'),
        widget=forms.TextInput(attrs={
            'autofocus': True,
            'autocomplete': 'username',
            'autocapitalize': 'none',
            'autocorrect': 'off',
            'spellcheck': 'false',
            'enterkeyhint': 'next',
            'placeholder': _('e.g. supervisor1'),
        }),
    )
    password = forms.CharField(
        label=_('Password'),
        widget=forms.PasswordInput(attrs={
            'autocomplete': 'current-password',
            'enterkeyhint': 'go',
            'placeholder': '••••••••',
        }),
    )

    def clean_username(self):
        """Staff type their username on a phone; tolerate stray spaces."""
        return self.cleaned_data['username'].strip()

    def add_error(self, field, error):
        """Flag the offending inputs so screen readers announce them as invalid."""
        super().add_error(field, error)
        targets = [field] if field else list(self.fields)
        for name in targets:
            if name in self.fields:
                self.fields[name].widget.attrs['aria-invalid'] = 'true'


class StaffForm(forms.ModelForm):
    password = forms.CharField(
        label=_('Password'), required=False, widget=forms.PasswordInput,
        help_text=_('Leave blank to keep the current password.'),
    )

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'phone', 'job_title',
                  'language', 'avatar', 'is_active']
        labels = {'is_active': _('Account active')}

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        if self.instance.pk is None:
            self.fields['password'].required = True

    def clean_username(self):
        username = self.cleaned_data['username'].strip().lower()
        clash = User.objects.filter(username__iexact=username).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(_('That username is already taken.'))
        return username

    def clean(self):
        data = super().clean()
        if data.get('is_active') and self.tenant is not None:
            live = User.objects.filter(
                tenant=self.tenant, role=User.ROLE_STAFF, is_active=True
            ).exclude(pk=self.instance.pk).count()
            if live + 1 > self.tenant.max_staff_seats:
                raise forms.ValidationError(_(
                    'Your plan allows %(n)d active staff seats. Deactivate someone first.'
                ) % {'n': self.tenant.max_staff_seats})
        return data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.ROLE_STAFF
        user.tenant = self.tenant
        password = self.cleaned_data.get('password')
        if password:
            user.set_password(password)
        if commit:
            user.save()
        return user


class PermissionForm(forms.Form):
    """A checkbox grid: one row per module, one column per action."""

    def __init__(self, *args, staff=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.staff = staff
        current = {p.module: p for p in staff.module_permissions.all()} if staff else {}
        for module, label in STAFF_MODULES:
            perm = current.get(module)
            for action in ['view', 'create', 'edit', 'delete']:
                self.fields[f'{module}__{action}'] = forms.BooleanField(
                    required=False,
                    initial=getattr(perm, f'can_{action}', False),
                    label=f'{label} — {action}',
                )

    COLUMNS = [_('View'), _('Create'), _('Edit'), _('Delete')]

    def columns(self):
        return self.COLUMNS

    def rows(self):
        for module, label in STAFF_MODULES:
            yield {
                'module': module,
                'label': label,
                'fields': [self[f'{module}__{action}'] for action in
                           ['view', 'create', 'edit', 'delete']],
            }

    def save(self):
        for module, _label in STAFF_MODULES:
            values = {
                f'can_{action}': self.cleaned_data.get(f'{module}__{action}', False)
                for action in ['view', 'create', 'edit', 'delete']
            }
            if any(values.values()):
                StaffPermission.objects.update_or_create(
                    user=self.staff, module=module, defaults=values
                )
            else:
                StaffPermission.objects.filter(user=self.staff, module=module).delete()


class AssignmentForm(forms.ModelForm):
    class Meta:
        model = StaffAssignment
        fields = ['building', 'floor', 'apartment']

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['building'].queryset = Building.objects.filter(
            tenant=tenant, is_archived=False)
        self.fields['floor'].queryset = Floor.objects.filter(
            tenant=tenant, is_archived=False)
        self.fields['apartment'].queryset = Apartment.objects.filter(
            tenant=tenant, is_archived=False).select_related('floor')
        self.fields['floor'].required = False
        self.fields['apartment'].required = False

    def clean(self):
        data = super().clean()
        building, floor = data.get('building'), data.get('floor')
        apartment = data.get('apartment')
        if floor and building and floor.building_id != building.id:
            raise forms.ValidationError(_('That floor is not in the selected building.'))
        if apartment and floor and apartment.floor_id != floor.id:
            raise forms.ValidationError(_('That apartment is not on the selected floor.'))
        if apartment and not floor and building and apartment.floor.building_id != building.id:
            raise forms.ValidationError(
                _('That apartment is not in the selected building.'))
        return data


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone', 'language', 'avatar']


class PasswordChangeSimpleForm(forms.Form):
    current_password = forms.CharField(label=_('Current password'), widget=forms.PasswordInput)
    new_password = forms.CharField(label=_('New password'), widget=forms.PasswordInput)
    confirm_password = forms.CharField(label=_('Confirm new password'), widget=forms.PasswordInput)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_current_password(self):
        value = self.cleaned_data['current_password']
        if not self.user.check_password(value):
            raise forms.ValidationError(_('That is not your current password.'))
        return value

    def clean(self):
        data = super().clean()
        if data.get('new_password') != data.get('confirm_password'):
            raise forms.ValidationError(_('The two new passwords do not match.'))
        from django.contrib.auth.password_validation import validate_password
        if data.get('new_password'):
            validate_password(data['new_password'], self.user)
        return data
