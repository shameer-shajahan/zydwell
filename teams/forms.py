from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from guests.models import Guest, Tenancy
from properties.models import Apartment

from .models import TeamLet

DATE = forms.DateInput(attrs={'type': 'date'})


class TeamLetForm(forms.Form):
    """Create or edit a team let. The company can be picked or typed in new."""

    company = forms.ModelChoiceField(
        label=_('Company'), queryset=Guest.objects.none(), required=False,
        empty_label=_('— New company —'),
    )
    company_name = forms.CharField(label=_('New company name'), max_length=150, required=False)
    collector_name = forms.CharField(label=_('Collector name'), max_length=150, required=False)
    collector_mobile = forms.CharField(label=_('Collector mobile'), max_length=32,
                                       required=False)
    apartment = forms.ModelChoiceField(label=_('Apartment'), queryset=Apartment.objects.none())
    team_name = forms.CharField(label=_('Team name'), max_length=150)
    joining_date = forms.DateField(label=_('Joining date'), required=False, widget=DATE,
                                   help_text=_('Leave empty when not known.'))
    billing_day = forms.IntegerField(label=_('Rent day of month'), min_value=1, max_value=28,
                                     initial=1, help_text=_('The invoice is raised on this day.'))
    members = forms.IntegerField(label=_('Members'), min_value=0, initial=0,
                                 help_text=_('May be more than the beds in the flat.'))
    per_head = forms.DecimalField(label=_('Rate per head'), max_digits=10, decimal_places=2,
                                  required=False, min_value=Decimal('0'))
    monthly_rent = forms.DecimalField(label=_('Agreed monthly rent'), max_digits=10,
                                      decimal_places=2, min_value=Decimal('0'),
                                      help_text=_('This is the amount billed each month.'))
    deposit_amount = forms.DecimalField(label=_('Security deposit agreed'), max_digits=10,
                                        decimal_places=2, initial=Decimal('0'), required=False,
                                        min_value=Decimal('0'))
    deposit_received = forms.DecimalField(label=_('Deposit received now'), max_digits=10,
                                          decimal_places=2, initial=Decimal('0'),
                                          required=False, min_value=Decimal('0'))
    prorate_first_period = forms.BooleanField(
        label=_('Charge only the days stayed in the first month'), required=False)
    agreement_note = forms.CharField(label=_('Note'), required=False,
                                     widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, tenant=None, user=None, team=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant, self.team = tenant, team
        self.fields['company'].queryset = Guest.objects.filter(tenant=tenant, is_company=True)
        flats = Apartment.objects.filter(tenant=tenant, is_archived=False).select_related(
            'floor', 'floor__building')
        if user is not None and user.is_staff_member:
            flats = user.scope_queryset(flats, building_field='floor__building',
                                        floor_field='floor', apartment_field='pk')
        self.fields['apartment'].queryset = flats
        self.fields['apartment'].label_from_instance = (
            lambda flat: f'{flat.floor.building.name} · {flat.floor.name} · {flat.name}')
        self.fields['per_head'].widget.attrs['data-team-per-head'] = '1'
        self.fields['members'].widget.attrs['data-team-members'] = '1'
        self.fields['monthly_rent'].widget.attrs['data-team-rent'] = '1'
        if team is not None:
            # A deposit taken later is recorded on the team page, not by editing.
            del self.fields['deposit_received']

    COMPANY_FIELDS = ['company', 'company_name', 'collector_name', 'collector_mobile']
    MONEY_FIELDS = ['members', 'per_head', 'monthly_rent', 'billing_day', 'deposit_amount',
                    'deposit_received', 'prorate_first_period']

    def company_fields(self):
        return [self[n] for n in self.COMPANY_FIELDS]

    def money_fields(self):
        return [self[n] for n in self.MONEY_FIELDS if n in self.fields]

    def place_fields(self):
        skip = set(self.COMPANY_FIELDS) | set(self.MONEY_FIELDS)
        return [self[n] for n in self.fields if n not in skip]

    @classmethod
    def initial_for(cls, team):
        return {
            'company': team.guest_id, 'apartment': team.apartment_id,
            'team_name': team.team_name, 'joining_date': team.joining_date,
            'billing_day': team.billing_day, 'members': team.members,
            'per_head': team.per_head, 'monthly_rent': team.monthly_rent,
            'deposit_amount': team.deposit_amount, 'agreement_note': team.agreement_note,
            'prorate_first_period': team.prorate_first_period,
        }

    def clean(self):
        data = super().clean()
        if not data.get('company') and not (data.get('company_name') or '').strip():
            self.add_error('company_name', _('Pick a company above or type a new one here.'))
        flat = data.get('apartment')
        if flat is not None and (self.team is None or flat.pk != self.team.apartment_id):
            busy = Tenancy.objects.filter(
                apartment=flat, room__isnull=False,
                status__code__in=Tenancy.LIVE_STATUS_CODES).select_related('guest').first()
            if busy:
                self.add_error('apartment', _(
                    '%(name)s is still checked in to this flat one by one. Check them out '
                    'or move them first.') % {'name': busy.guest.full_name})
        if flat is not None and data.get('team_name'):
            same = TeamLet.objects.live().filter(apartment=flat,
                                                 team_name__iexact=data['team_name'].strip())
            if self.team is not None:
                same = same.exclude(pk=self.team.pk)
            if same.exists():
                self.add_error('team_name', _('A team of this name is already in this flat.'))
        return data

    def headcount_note(self):
        """Members × per-head when the agreed rent differs — shown, never blocking."""
        data = getattr(self, 'cleaned_data', None) if self.is_bound else self.initial
        data = data or {}
        per_head, members, rent = data.get('per_head'), data.get('members'), \
            data.get('monthly_rent')
        if per_head is None or members is None or rent is None:
            return None
        expected = per_head * members
        return expected if expected != rent else None


class TeamImportForm(forms.Form):
    file = forms.FileField(label=_('CSV file'),
                           help_text=_('Comma-separated, first row the column names.'))
