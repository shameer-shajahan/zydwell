from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from properties.models import Bed, Room

from .models import Guest, GuestDocument, Tenancy


class GuestForm(forms.ModelForm):
    class Meta:
        model = Guest
        fields = ['full_name', 'mobile', 'whatsapp_number', 'email', 'photo', 'nationality',
                  'date_of_birth', 'gender', 'emergency_contact_name', 'emergency_contact_number',
                  'employer', 'employer_phone', 'notes']
        widgets = {
            'date_of_birth': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }


class GuestDocumentForm(forms.ModelForm):
    class Meta:
        model = GuestDocument
        fields = ['doc_type', 'number', 'issue_date', 'expiry_date', 'scan', 'notes']
        widgets = {
            'issue_date': forms.DateInput(attrs={'type': 'date'}),
            'expiry_date': forms.DateInput(attrs={'type': 'date'}),
        }


class CheckInForm(forms.Form):
    guest = forms.ModelChoiceField(label=_('Guest'), queryset=Guest.objects.none())
    room = forms.ModelChoiceField(label=_('Room'), queryset=Room.objects.none())
    bed = forms.ModelChoiceField(label=_('Bed'), queryset=Bed.objects.none(), required=False,
                                 help_text=_('Leave empty when the whole room or flat is let.'))
    check_in_date = forms.DateField(label=_('Check-in date'), initial=timezone.localdate,
                                    widget=forms.DateInput(attrs={'type': 'date'}))
    expected_check_out = forms.DateField(label=_('Expected check-out'), required=False,
                                         widget=forms.DateInput(attrs={'type': 'date'}))
    monthly_rent = forms.DecimalField(label=_('Agreed monthly rent'), max_digits=10, decimal_places=2)
    billing_cycle = forms.ChoiceField(label=_('Billing cycle'), choices=Tenancy.CYCLE_CHOICES,
                                      initial=Tenancy.CYCLE_MONTHLY)
    billing_day = forms.IntegerField(label=_('Invoice day of month'), min_value=1, max_value=28,
                                     initial=1)
    rent_mode = forms.ChoiceField(
        label=_('Billing arrangement'), choices=Tenancy.RENT_MODE_CHOICES,
        initial=Tenancy.MODE_ADVANCE,
    )
    first_charge = forms.ChoiceField(
        label=_('First invoice'),
        choices=[
            ('full', _('Full rent for the period — collect the whole amount now')),
            ('prorata', _('Only the days stayed in this first period')),
            ('custom', _('A set amount we agreed')),
        ],
        initial='full',
    )
    first_amount = forms.DecimalField(
        label=_('Amount agreed for the first invoice'), max_digits=10, decimal_places=2,
        required=False, help_text=_('Only used when "a set amount" is chosen above.'),
    )
    deposit_amount = forms.DecimalField(label=_('Security deposit agreed'), max_digits=10,
                                        decimal_places=2, initial=Decimal('0'))
    deposit_received = forms.DecimalField(label=_('Deposit received now'), max_digits=10,
                                          decimal_places=2, initial=Decimal('0'))
    raise_first_invoice = forms.BooleanField(
        label=_('Raise the first invoice now (pro-rata)'), required=False, initial=True
    )
    agreement_note = forms.CharField(label=_('Agreement note'), required=False,
                                     widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, tenant=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        from properties.models import visible_beds

        self.fields['guest'].queryset = Guest.objects.filter(tenant=tenant, is_blacklisted=False)
        rooms = Room.objects.filter(tenant=tenant, is_archived=False).select_related(
            'apartment', 'apartment__floor', 'apartment__floor__building'
        )
        if user is not None and user.is_staff_member:
            rooms = user.scope_queryset(
                rooms, building_field='apartment__floor__building',
                floor_field='apartment__floor', apartment_field='apartment')
        self.fields['room'].queryset = rooms
        beds = visible_beds(user, tenant) if user else Bed.objects.filter(tenant=tenant)
        self.fields['bed'].queryset = beds
        self.fields['bed'].widget.attrs['data-parent-field'] = 'room'

    def clean(self):
        data = super().clean()
        room, bed = data.get('room'), data.get('bed')
        if bed and room and bed.room_id != room.id:
            self.add_error('bed', _('That bed is not in the selected room.'))
        if room and not bed and not room.lets_whole_unit:
            self.add_error('bed', _('Pick a bed — this room is let per bed.'))
        if bed:
            clash = Tenancy.objects.filter(
                bed=bed, status__code__in=Tenancy.LIVE_STATUS_CODES
            ).first()
            if clash:
                self.add_error('bed', _('%(name)s is already in this bed.') % {
                    'name': clash.guest.full_name})
        if room and room.lets_whole_unit and not bed:
            clash = Tenancy.objects.filter(
                room=room, bed__isnull=True, status__code__in=Tenancy.LIVE_STATUS_CODES
            ).first()
            if clash:
                self.add_error('room', _('This unit is already let to %(name)s.') % {
                    'name': clash.guest.full_name})
        if data.get('first_charge') == 'custom' and data.get('first_amount') in (None, ''):
            self.add_error('first_amount', _('Enter the amount you agreed with the guest.'))
        return data

    def billing_choices(self):
        """``(prorate_first_period, first_invoice_amount)`` from the radio choice."""
        choice = self.cleaned_data.get('first_charge')
        if choice == 'custom':
            return False, self.cleaned_data.get('first_amount')
        return choice == 'prorata', None


class TenancyBillingForm(forms.ModelForm):
    """Admin-side changes to how a live tenancy is billed."""

    class Meta:
        model = Tenancy
        fields = ['rent_mode', 'prorate_first_period', 'first_invoice_amount']


class CheckOutForm(forms.Form):
    check_out_date = forms.DateField(label=_('Check-out date'), initial=timezone.localdate,
                                     widget=forms.DateInput(attrs={'type': 'date'}))
    deduction_amount = forms.DecimalField(label=_('Deduct from deposit'), max_digits=10,
                                          decimal_places=2, required=False, initial=Decimal('0'))
    deduction_reason = forms.CharField(label=_('Reason for deduction'), required=False,
                                       max_length=200)
    refund_amount = forms.DecimalField(label=_('Refund to guest'), max_digits=10, decimal_places=2,
                                       required=False, initial=Decimal('0'))
    note = forms.CharField(label=_('Note'), required=False, max_length=200)

    def __init__(self, *args, tenancy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenancy = tenancy
        if tenancy is not None:
            self.fields['refund_amount'].initial = tenancy.deposit_held()

    def clean(self):
        data = super().clean()
        held = self.tenancy.deposit_held() if self.tenancy else Decimal('0')
        total_out = (data.get('deduction_amount') or Decimal('0')) + \
                    (data.get('refund_amount') or Decimal('0'))
        if total_out > held:
            raise forms.ValidationError(_(
                'Deductions plus refund (%(out)s) exceed the deposit held (%(held)s).'
            ) % {'out': total_out, 'held': held})
        if data.get('deduction_amount') and not data.get('deduction_reason'):
            self.add_error('deduction_reason', _('Give a reason for the deduction.'))
        return data


class TransferForm(forms.Form):
    new_room = forms.ModelChoiceField(label=_('Move to room'), queryset=Room.objects.none())
    new_bed = forms.ModelChoiceField(label=_('Move to bed'), queryset=Bed.objects.none(),
                                     required=False)
    transfer_date = forms.DateField(label=_('Transfer date'), initial=timezone.localdate,
                                    widget=forms.DateInput(attrs={'type': 'date'}))
    new_rent = forms.DecimalField(label=_('New monthly rent'), max_digits=10, decimal_places=2,
                                  required=False,
                                  help_text=_('Leave empty to take the new bed or room price.'))
    reason = forms.CharField(label=_('Reason'), required=False, max_length=200)

    def __init__(self, *args, tenancy=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenancy = tenancy
        tenant = tenancy.tenant
        rooms = Room.objects.filter(tenant=tenant, is_archived=False).select_related(
            'apartment', 'apartment__floor', 'apartment__floor__building')
        beds = Bed.objects.filter(tenant=tenant, is_archived=False).select_related('room')
        if user is not None and user.is_staff_member:
            rooms = user.scope_queryset(
                rooms, building_field='apartment__floor__building',
                floor_field='apartment__floor', apartment_field='apartment')
            beds = beds.filter(room__in=rooms)
        self.fields['new_room'].queryset = rooms
        self.fields['new_bed'].queryset = beds

    def clean(self):
        data = super().clean()
        room, bed = data.get('new_room'), data.get('new_bed')
        if bed and room and bed.room_id != room.id:
            self.add_error('new_bed', _('That bed is not in the selected room.'))
        if bed and bed.pk == self.tenancy.bed_id:
            self.add_error('new_bed', _('The guest is already in this bed.'))
        if bed:
            clash = Tenancy.objects.filter(
                bed=bed, status__code__in=Tenancy.LIVE_STATUS_CODES
            ).exclude(pk=self.tenancy.pk).first()
            if clash:
                self.add_error('new_bed', _('%(name)s is already in that bed.') % {
                    'name': clash.guest.full_name})
        if room and not bed and not room.lets_whole_unit:
            self.add_error('new_bed', _('Pick a bed — that room is let per bed.'))
        return data


class NoticeForm(forms.Form):
    notice_given_on = forms.DateField(label=_('Notice given on'), initial=timezone.localdate,
                                      widget=forms.DateInput(attrs={'type': 'date'}))
    expected_check_out = forms.DateField(label=_('Leaving on'), required=False,
                                         widget=forms.DateInput(attrs={'type': 'date'}))


class TenancyEditForm(forms.ModelForm):
    """Rent edits are admin-only and always land in the audit log."""

    class Meta:
        model = Tenancy
        fields = ['monthly_rent', 'rent_mode', 'prorate_first_period', 'billing_cycle',
                  'billing_day', 'expected_check_out', 'deposit_amount',
                  'first_invoice_amount', 'agreement_note']
        widgets = {
            'expected_check_out': forms.DateInput(attrs={'type': 'date'}),
            'agreement_note': forms.Textarea(attrs={'rows': 2}),
        }


class DepositForm(forms.Form):
    amount = forms.DecimalField(label=_('Amount received'), max_digits=10, decimal_places=2)
    mode = forms.ModelChoiceField(label=_('Received as'), queryset=None, required=False)
    reference = forms.CharField(label=_('Reference'), required=False, max_length=80)
    reason = forms.CharField(label=_('Note'), required=False, max_length=200)

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        from billing.models import PaymentMode

        modes = PaymentMode.objects.filter(tenant=tenant, is_active=True)
        self.fields['mode'].queryset = modes
        default = PaymentMode.by_code(tenant, 'cash') if tenant else None
        if default:
            self.fields['mode'].initial = default.pk

    def clean(self):
        data = super().clean()
        mode = data.get('mode')
        if mode and mode.needs_reference and not data.get('reference'):
            self.add_error('reference', _('Add the %(mode)s reference.') % {
                'mode': mode.name.lower()})
        return data


MAX_UPLOAD = 5 * 1024 * 1024   # 5 MB — these arrive from an unauthenticated page
ALLOWED_UPLOADS = {'.jpg', '.jpeg', '.png', '.heic', '.webp', '.pdf'}


def _check_upload(uploaded):
    if uploaded is None:
        return uploaded
    import os

    extension = os.path.splitext(uploaded.name)[1].lower()
    if extension not in ALLOWED_UPLOADS:
        raise forms.ValidationError(
            _('Upload a photo or a PDF (%(kinds)s).')
            % {'kinds': ', '.join(sorted(ALLOWED_UPLOADS))}
        )
    if uploaded.size > MAX_UPLOAD:
        raise forms.ValidationError(_('That file is larger than 5 MB. Try a photo instead.'))
    return uploaded


class GuestSelfProfileForm(forms.ModelForm):
    """What a guest may fill in about themselves from a shared link.

    Deliberately excludes anything the office owns — rent, bed, deposit,
    blacklisting, notes. Name, mobile, WhatsApp, gender and passport number are
    required; everything else is optional so a guest can come back later.
    """

    REQUIRED = {'full_name', 'mobile', 'whatsapp_number', 'gender', 'passport_number'}

    passport_number = forms.CharField(label=_('Passport number'), required=False, max_length=60)
    passport_expiry = forms.DateField(label=_('Passport expiry'), required=False,
                                      widget=forms.DateInput(attrs={'type': 'date'}))
    passport_scan = forms.FileField(label=_('Photo of your passport'), required=False)

    emirates_id_number = forms.CharField(label=_('Emirates ID number'), required=False,
                                         max_length=60)
    emirates_id_expiry = forms.DateField(label=_('Emirates ID expiry'), required=False,
                                         widget=forms.DateInput(attrs={'type': 'date'}))
    emirates_id_scan = forms.FileField(label=_('Photo of your Emirates ID'), required=False)

    visa_number = forms.CharField(label=_('Visa number'), required=False, max_length=60)
    visa_expiry = forms.DateField(label=_('Visa expiry'), required=False,
                                  widget=forms.DateInput(attrs={'type': 'date'}))
    visa_scan = forms.FileField(label=_('Photo of your visa'), required=False)

    DOCUMENTS = [
        ('passport', GuestDocument.TYPE_PASSPORT),
        ('emirates_id', GuestDocument.TYPE_EMIRATES_ID),
        ('visa', GuestDocument.TYPE_VISA),
    ]

    class Meta:
        model = Guest
        fields = ['full_name', 'mobile', 'whatsapp_number', 'email', 'photo',
                  'nationality', 'date_of_birth', 'gender',
                  'emergency_contact_name', 'emergency_contact_number',
                  'employer', 'employer_phone']
        widgets = {'date_of_birth': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name in self.REQUIRED
        self.fields['mobile'].help_text = _('So the office can reach you.')
        self.fields['whatsapp_number'].help_text = _(
            'Can be the same as your mobile number.')
        self.fields['photo'].label = _('Your photo')
        for prefix, doc_type in self.DOCUMENTS:
            existing = self.instance.documents.filter(doc_type=doc_type).first() \
                if self.instance.pk else None
            if existing:
                self.fields[f'{prefix}_number'].initial = existing.number
                self.fields[f'{prefix}_expiry'].initial = existing.expiry_date

    def clean_photo(self):
        return _check_upload(self.cleaned_data.get('photo'))

    def clean_passport_scan(self):
        return _check_upload(self.cleaned_data.get('passport_scan'))

    def clean_emirates_id_scan(self):
        return _check_upload(self.cleaned_data.get('emirates_id_scan'))

    def clean_visa_scan(self):
        return _check_upload(self.cleaned_data.get('visa_scan'))

    def clean(self):
        data = super().clean()
        for prefix, _doc_type in self.DOCUMENTS:
            if data.get(f'{prefix}_expiry') and not data.get(f'{prefix}_number'):
                self.add_error(f'{prefix}_number',
                               _('Add the number as well as the expiry date.'))
        return data

    def save(self, commit=True):
        guest = super().save(commit=commit)
        for prefix, doc_type in self.DOCUMENTS:
            number = self.cleaned_data.get(f'{prefix}_number')
            expiry = self.cleaned_data.get(f'{prefix}_expiry')
            scan = self.cleaned_data.get(f'{prefix}_scan')
            if not (number or expiry or scan):
                continue
            document = guest.documents.filter(doc_type=doc_type).first()
            if document is None:
                document = GuestDocument(tenant=guest.tenant, guest=guest,
                                         doc_type=doc_type, number=number or '')
            if number:
                document.number = number
            if expiry:
                document.expiry_date = expiry
                document.last_alert_day_bucket = None   # re-arm the expiry alerts
            if scan:
                document.scan = scan
            document.save()
        return guest

    def sections(self):
        """Grouped for the page, so a guest is not faced with one long column."""
        def fields(*names):
            return [self[name] for name in names]

        return [
            (_('About you'), fields('full_name', 'date_of_birth', 'nationality', 'gender',
                                    'photo')),
            (_('How we reach you'), fields('mobile', 'whatsapp_number', 'email')),
            (_('In an emergency'), fields('emergency_contact_name',
                                          'emergency_contact_number')),
            (_('Where you work'), fields('employer', 'employer_phone')),
            (_('Your documents'), fields('passport_number', 'passport_expiry', 'passport_scan',
                                         'emirates_id_number', 'emirates_id_expiry',
                                         'emirates_id_scan',
                                         'visa_number', 'visa_expiry', 'visa_scan')),
        ]


class ShareProfileLinkForm(forms.Form):
    days = forms.ChoiceField(
        label=_('Link works for'),
        choices=[('1', _('1 day')), ('7', _('7 days')), ('30', _('30 days'))],
        initial='7',
    )
