from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounting.models import KIND_INCOME, Category
from guests.models import Tenancy

from .models import (
    CashHandover, Invoice, LateFeeRule, Payment, PaymentMode, PrinterConfig,
)


class InvoiceMultiSelect(forms.SelectMultiple):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index,
                                       subindex=subindex, attrs=attrs)
        invoice = getattr(value, 'instance', None)
        if invoice is not None:
            option['attrs']['data-tenancy'] = str(invoice.tenancy_id)
            option['attrs']['data-balance'] = str(invoice.balance)
        return option


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
    invoices = forms.ModelMultipleChoiceField(
        label=_('Invoice(s)'), queryset=Invoice.objects.none(), required=False,
        widget=InvoiceMultiSelect(attrs={'size': 7, 'data-invoice-picker': '1'}),
        help_text=_('Select one or more open invoices for this tenancy.'))

    # Keep accepting old links and forms which post a single invoice id.
    invoice = forms.ModelChoiceField(queryset=Invoice.objects.none(), required=False,
                                     widget=forms.HiddenInput)

    class Meta:
        model = Payment
        fields = ['tenancy', 'invoice', 'invoices', 'amount', 'mode', 'reference', 'cheque_due_date',
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
        tenancies = tenancies.filter(status__code__in=Tenancy.LIVE_STATUS_CODES)
        self.fields['tenancy'].queryset = tenancies
        self.fields['tenancy'].widget.attrs['data-invoice-tenancy'] = '1'
        invoices = Invoice.objects.filter(tenant=tenant).exclude(
            status__counts_as_settled=True
        ).select_related('tenancy__guest')
        self.fields['invoice'].queryset = invoices
        self.fields['invoices'].queryset = invoices.filter(tenancy__in=tenancies)
        self.fields['invoices'].label_from_instance = (
            lambda inv: f'{inv.number} · {inv.period_label()} · {inv.balance:,.2f}')
        self.fields['invoices'].help_text = _(
            'Leave empty to hold the money as advance credit.')
        if self.initial.get('invoice') and not self.initial.get('invoices'):
            self.initial['invoices'] = [self.initial['invoice']]
        self.fields['paid_on'].initial = timezone.localdate()
        self.fields['mode'].queryset = PaymentMode.objects.filter(
            tenant=tenant, is_active=True)
        self.fields['mode'].empty_label = None
        default = PaymentMode.by_code(tenant, 'cash') or PaymentMode.default_for(tenant)
        if default and not self.initial.get('mode'):
            self.fields['mode'].initial = default.pk

    def clean(self):
        data = super().clean()
        selected = list(data.get('invoices') or [])
        legacy_invoice, tenancy = data.get('invoice'), data.get('tenancy')
        if not selected and legacy_invoice:
            selected = [legacy_invoice]
        data['invoices'] = selected
        if selected and tenancy and any(inv.tenancy_id != tenancy.pk for inv in selected):
            self.add_error('invoices', _('Select invoices for this same tenancy.'))
        mode = data.get('mode')
        if mode and mode.needs_reference and not data.get('reference'):
            self.add_error('reference', _('Add the %(mode)s reference.') % {
                'mode': mode.name.lower()})
        if mode and mode.needs_due_date and not data.get('cheque_due_date'):
            self.add_error('cheque_due_date', _('Record when it falls due.'))
        amount = data.get('amount') or Decimal('0')
        available = sum((inv.balance for inv in selected), Decimal('0'))
        if selected and amount > available:
            self.add_error('amount', _(
                'That is more than the %(bal)s outstanding on the selected invoices.'
            ) % {'bal': available})
        if mode and mode.settles_from_credit:
            if not selected:
                self.add_error('invoices', _('Select invoices to apply advance credit to.'))
            elif tenancy and amount > tenancy.guest.advance_credit():
                self.add_error('amount', _('The tenant does not have that much advance credit.'))
        return data


class AdvancePaymentForm(forms.ModelForm):
    """Rent for the next period, taken before it falls due.

    The guest and the period are fixed by the page; only the money is asked for.
    """

    class Meta:
        model = Payment
        fields = ['amount', 'mode', 'reference', 'cheque_due_date', 'paid_on', 'note']
        widgets = {
            'paid_on': forms.DateInput(attrs={'type': 'date'}),
            'cheque_due_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, tenant=None, rent=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rent = rent
        self.fields['amount'].initial = rent
        self.fields['amount'].help_text = _('Less than the full rent leaves the rest owing.')
        self.fields['paid_on'].initial = timezone.localdate()
        self.fields['mode'].queryset = PaymentMode.objects.filter(
            tenant=tenant, is_active=True)
        self.fields['mode'].empty_label = None
        default = PaymentMode.by_code(tenant, 'cash') or PaymentMode.default_for(tenant)
        if default:
            self.fields['mode'].initial = default.pk

    def clean(self):
        data = super().clean()
        mode = data.get('mode')
        if mode and mode.needs_reference and not data.get('reference'):
            self.add_error('reference', _('Add the %(mode)s reference.') % {
                'mode': mode.name.lower()})
        if mode and mode.needs_due_date and not data.get('cheque_due_date'):
            self.add_error('cheque_due_date', _('Record when it falls due.'))
        if mode and mode.settles_from_credit:
            self.add_error('mode', _('Use “Apply credit” on the invoice to spend advance credit.'))
        amount = data.get('amount')
        if amount is not None and amount <= 0:
            self.add_error('amount', _('Enter an amount greater than zero.'))
        elif amount is not None and self.rent is not None and amount > self.rent:
            self.add_error('amount', _(
                'That is more than the %(rent)s rent for this period. '
                'Collect the following period separately.') % {'rent': self.rent})
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
