from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounting.models import KIND_INCOME, Category
from guests.models import Tenancy

from .models import (
    CashHandover, Invoice, LateFeeRule, Payment, PaymentMode, PrinterConfig,
)


class InvoiceForm(forms.Form):
    tenancy = forms.ModelChoiceField(label=_('Tenancy'), queryset=Tenancy.objects.none())
    period_start = forms.DateField(label=_('Period from'),
                                   widget=forms.DateInput(attrs={'type': 'date'}))
    period_end = forms.DateField(label=_('Period to'),
                                 widget=forms.DateInput(attrs={'type': 'date'}))
    due_date = forms.DateField(label=_('Due date'), widget=forms.DateInput(attrs={'type': 'date'}))
    rent_amount = forms.DecimalField(label=_('Rent'), max_digits=10, decimal_places=2)
    extra_category = forms.ModelChoiceField(label=_('Extra charge category'),
                                            queryset=Category.objects.none(), required=False)
    extra_description = forms.CharField(label=_('Extra charge description'), required=False,
                                        max_length=160)
    extra_amount = forms.DecimalField(label=_('Extra charge amount'), max_digits=10,
                                      decimal_places=2, required=False)
    notes = forms.CharField(label=_('Note on the invoice'), required=False, max_length=200)

    def __init__(self, *args, tenant=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Tenancy.objects.filter(
            tenant=tenant, status__code__in=Tenancy.LIVE_STATUS_CODES
        ).select_related('guest', 'room')
        if user is not None:
            qs = user.scope_queryset(qs)
        self.fields['tenancy'].queryset = qs
        self.fields['extra_category'].queryset = Category.objects.filter(
            tenant=tenant, kind=KIND_INCOME, is_active=True
        )

    def clean(self):
        data = super().clean()
        if data.get('period_start') and data.get('period_end'):
            if data['period_end'] < data['period_start']:
                self.add_error('period_end', _('The period end is before its start.'))
        if data.get('extra_amount') and not data.get('extra_category'):
            self.add_error('extra_category', _('Pick a category for the extra charge.'))
        tenancy, start = data.get('tenancy'), data.get('period_start')
        if tenancy and start and Invoice.objects.filter(
            tenancy=tenancy, period_start=start
        ).exists():
            self.add_error('period_start', _('An invoice already covers that period.'))
        return data


class DiscountForm(forms.Form):
    """Admin approval is required — the form is only shown to admins."""

    discount_amount = forms.DecimalField(label=_('Discount / waiver'), max_digits=10,
                                         decimal_places=2)
    discount_reason = forms.CharField(label=_('Reason'), max_length=200)


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ['tenancy', 'invoice', 'amount', 'mode', 'reference', 'cheque_due_date',
                  'paid_on', 'note']
        widgets = {
            'paid_on': forms.DateInput(attrs={'type': 'date'}),
            'cheque_due_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, tenant=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        tenancies = Tenancy.objects.filter(tenant=tenant).select_related('guest', 'room')
        if user is not None:
            tenancies = user.scope_queryset(tenancies)
        self.fields['tenancy'].queryset = tenancies.filter(
            status__code__in=Tenancy.LIVE_STATUS_CODES
        )
        invoices = Invoice.objects.filter(tenant=tenant).exclude(
            status__counts_as_settled=True
        ).select_related('tenancy__guest')
        self.fields['invoice'].queryset = invoices
        self.fields['invoice'].required = False
        self.fields['invoice'].help_text = _('Leave empty to hold the money as advance credit.')
        self.fields['paid_on'].initial = timezone.localdate()
        self.fields['invoice'].widget.attrs['data-balance-source'] = '#id_amount'
        self.fields['mode'].queryset = PaymentMode.objects.filter(
            tenant=tenant, is_active=True)
        self.fields['mode'].empty_label = None
        default = PaymentMode.by_code(tenant, 'cash') or PaymentMode.default_for(tenant)
        if default and not self.initial.get('mode'):
            self.fields['mode'].initial = default.pk

    def clean(self):
        data = super().clean()
        invoice, tenancy = data.get('invoice'), data.get('tenancy')
        if invoice and tenancy and invoice.tenancy_id != tenancy.id:
            self.add_error('invoice', _('That invoice belongs to a different guest.'))
        mode = data.get('mode')
        if mode and mode.needs_reference and not data.get('reference'):
            self.add_error('reference', _('Add the %(mode)s reference.') % {
                'mode': mode.name.lower()})
        if mode and mode.needs_due_date and not data.get('cheque_due_date'):
            self.add_error('cheque_due_date', _('Record when it falls due.'))
        amount = data.get('amount') or Decimal('0')
        if invoice and amount > invoice.balance:
            self.add_error('amount', _(
                'That is more than the %(bal)s outstanding on this invoice. '
                'Record the excess as a separate advance payment.'
            ) % {'bal': invoice.balance})
        return data


class PaymentEditForm(forms.ModelForm):
    """Admin correction of a receipt already collected. Guest and invoice stay put."""

    class Meta:
        model = Payment
        fields = ['amount', 'mode', 'reference', 'cheque_due_date', 'paid_on', 'note']
        widgets = {
            'paid_on': forms.DateInput(attrs={'type': 'date'}),
            'cheque_due_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        payment = self.instance
        self.original_amount = payment.amount
        modes = PaymentMode.objects.filter(tenant=payment.tenant, is_active=True)
        if payment.mode_id:
            modes = modes | PaymentMode.objects.filter(pk=payment.mode_id)
        self.fields['mode'].queryset = modes.distinct()
        self.fields['mode'].empty_label = None
        if payment.from_credit:
            # Settled out of advance credit — switching it to cash would invent money.
            self.fields['mode'].disabled = True

    def clean(self):
        data = super().clean()
        payment = self.instance
        mode = data.get('mode')
        if mode and mode.needs_reference and not data.get('reference'):
            self.add_error('reference', _('Add the %(mode)s reference.') % {
                'mode': mode.name.lower()})
        if mode and mode.needs_due_date and not data.get('cheque_due_date'):
            self.add_error('cheque_due_date', _('Record when it falls due.'))
        amount = data.get('amount')
        if amount is None or amount <= 0:
            self.add_error('amount', _('Enter an amount greater than zero.'))
            return data
        change = amount - self.original_amount
        if payment.is_reversed:
            return data
        if payment.invoice_id and change > payment.invoice.balance:
            self.add_error('amount', _(
                'That is more than the invoice total. At most %(max)s fits.'
            ) % {'max': self.original_amount + payment.invoice.balance})
        if payment.from_credit and change > payment.tenancy.guest.advance_credit():
            self.add_error('amount', _('The guest does not have that much advance credit.'))
        if payment.invoice_id is None and change < 0 \
                and payment.tenancy.guest.advance_credit() + change < 0:
            self.add_error('amount', _(
                'Part of this advance has already been used as credit; it cannot go that low.'))
        return data


class InvoiceEditForm(forms.ModelForm):
    """Admin correction of an invoice's dates and amounts."""

    class Meta:
        model = Invoice
        fields = ['issue_date', 'period_start', 'period_end', 'due_date', 'rent_amount',
                  'late_fee', 'discount_amount', 'discount_reason', 'notes']
        widgets = {name: forms.DateInput(attrs={'type': 'date'})
                   for name in ['issue_date', 'period_start', 'period_end', 'due_date']}

    def clean(self):
        data = super().clean()
        start, end = data.get('period_start'), data.get('period_end')
        if start and end and end < start:
            self.add_error('period_end', _('The period cannot end before it starts.'))
        for name in ['rent_amount', 'late_fee', 'discount_amount']:
            if data.get(name) is not None and data[name] < 0:
                self.add_error(name, _('Cannot be negative.'))
        if data.get('discount_amount') and not data.get('discount_reason'):
            self.add_error('discount_reason', _('Say why the discount was given.'))
        if not self.errors:
            invoice = self.instance
            total = (data['rent_amount'] + invoice.extras_total + data['late_fee']
                     - data['discount_amount'])
            if total < invoice.paid_amount:
                self.add_error(None, _(
                    'The new total (%(total)s) is less than the %(paid)s already paid. '
                    'Edit or delete those payments first.'
                ) % {'total': total, 'paid': invoice.paid_amount})
        return data


class ReversalForm(forms.Form):
    reason = forms.CharField(label=_('Reason for reversal'), max_length=200,
                             widget=forms.Textarea(attrs={'rows': 2}))


class LateFeeRuleForm(forms.ModelForm):
    class Meta:
        model = LateFeeRule
        fields = ['name', 'grace_days', 'kind', 'value', 'max_amount', 'is_active']


class PrinterForm(forms.ModelForm):
    class Meta:
        model = PrinterConfig
        fields = ['name', 'paper_width', 'connection', 'address', 'copies', 'header_text',
                  'footer_text', 'is_default']


class HandoverForm(forms.ModelForm):
    class Meta:
        model = CashHandover
        fields = ['handover_date', 'declared_amount', 'note']
        widgets = {'handover_date': forms.DateInput(attrs={'type': 'date'})}


class PaymentModeForm(forms.ModelForm):
    class Meta:
        model = PaymentMode
        fields = ['name', 'code', 'counts_as_cash', 'needs_reference', 'needs_due_date',
                  'settles_from_credit', 'sort_order', 'is_active']


class ApprovalForm(forms.Form):
    note = forms.CharField(label=_('Note'), required=False, max_length=200)


class RejectionForm(forms.Form):
    reason = forms.CharField(label=_('Why is it being rejected?'), max_length=200,
                             widget=forms.Textarea(attrs={'rows': 2}))
