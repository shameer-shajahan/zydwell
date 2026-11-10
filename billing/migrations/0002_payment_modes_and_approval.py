"""Payment modes become data, and collected money can need approval.

The auto-detector wanted to alter Payment.mode straight from text to a foreign
key, which on a live database turns every 'cash' and 'cheque' into nothing. This
keeps the old column until its values have been mapped across.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0001_initial'),
        ('core', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]


#: The old hard-coded list, plus the two the owner asked for.
SEED = [
    # code, name, cash, reference, due date, from credit
    ('cash', 'Cash', True, False, False, False),
    ('card', 'Card', False, True, False, False),
    ('upi', 'UPI', False, True, False, False),
    ('bank', 'Bank transfer', False, True, False, False),
    ('cheque', 'Cheque', False, True, True, False),
    ('credit', 'Guest credit', False, False, False, True),
    ('online', 'Online / link', False, True, False, False),
]


def create_modes_and_map(apps, schema_editor):
    """Give every client the standard modes, then move payments onto them."""
    Tenant = apps.get_model('core', 'Tenant')
    PaymentMode = apps.get_model('billing', 'PaymentMode')
    Payment = apps.get_model('billing', 'Payment')

    for tenant in Tenant.objects.all():
        for order, (code, name, cash, reference, due, credit) in enumerate(SEED):
            PaymentMode.objects.get_or_create(
                tenant=tenant, code=code,
                defaults={'name': name, 'counts_as_cash': cash,
                          'needs_reference': reference, 'needs_due_date': due,
                          'settles_from_credit': credit, 'sort_order': order},
            )
        lookup = {mode.code: mode for mode in PaymentMode.objects.filter(tenant=tenant)}
        for code, mode in lookup.items():
            Payment.objects.filter(tenant=tenant, mode=code).update(mode_ref=mode)
        # Anything written by a older build with a code we no longer seed.
        Payment.objects.filter(tenant=tenant, mode_ref__isnull=True).update(
            mode_ref=lookup.get('cash'))


def unmap(apps, schema_editor):
    """Put the code back in the text column so the migration can be undone."""
    Payment = apps.get_model('billing', 'Payment')
    for payment in Payment.objects.exclude(mode_ref__isnull=True).select_related('mode_ref'):
        payment.mode = payment.mode_ref.code
        payment.save(update_fields=['mode'])


class Migration(migrations.Migration):
    dependencies = [
        ('billing', '0001_initial'),
        ('core', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentMode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=40)),
                ('code', models.SlugField()),
                ('counts_as_cash', models.BooleanField(default=False, help_text='Included in the daily cash handover to the owner.', verbose_name='counts as cash in hand')),
                ('needs_reference', models.BooleanField(default=False, help_text='Cheque number, UPI reference, card last four digits.', verbose_name='needs a reference')),
                ('needs_due_date', models.BooleanField(default=False, help_text='Post-dated cheques, so the office can be reminded.', verbose_name='needs a due date')),
                ('settles_from_credit', models.BooleanField(default=False, help_text='No new money — it spends credit the guest already paid in.', verbose_name='draws on the guest’s advance')),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='%(class)s_set', to='core.tenant')),
            ],
            options={
                'ordering': ['sort_order', 'name'],
            },
        ),
        migrations.AddField(
            model_name='payment',
            name='approval',
            field=models.CharField(choices=[('not_required', 'No approval needed'), ('pending', 'Waiting for the owner'), ('approved', 'Approved'), ('rejected', 'Rejected')], default='not_required', max_length=14, verbose_name='approval'),
        ),
        migrations.AddField(
            model_name='payment',
            name='approval_note',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='payment',
            name='approved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='payment',
            name='approved_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='payments_approved', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddConstraint(
            model_name='paymentmode',
            constraint=models.UniqueConstraint(fields=('tenant', 'code'), name='uniq_payment_mode'),
        ),
        migrations.AddField(
            model_name='payment',
            name='mode_ref',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='payments', to='billing.paymentmode'),
        ),
        migrations.RunPython(create_modes_and_map, unmap),
        migrations.RemoveField(model_name='payment', name='mode'),
        migrations.RenameField(model_name='payment', old_name='mode_ref', new_name='mode'),
    ]
