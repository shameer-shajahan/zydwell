from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0003_list_indexes'),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentAllocation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('invoice', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name='payment_allocations',
                                              to='billing.invoice')),
                ('payment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name='allocations',
                                              to='billing.payment')),
            ],
        ),
        migrations.AddConstraint(
            model_name='paymentallocation',
            constraint=models.UniqueConstraint(fields=('payment', 'invoice'),
                                               name='uniq_payment_invoice_allocation'),
        ),
        migrations.AddConstraint(
            model_name='paymentallocation',
            constraint=models.CheckConstraint(condition=models.Q(('amount__gt', 0)),
                                              name='payment_allocation_amount_positive'),
        ),
    ]
