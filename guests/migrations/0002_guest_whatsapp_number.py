"""Repurpose the alternate mobile as the WhatsApp number.

A rename, not a drop-and-create: the auto-detector proposed removing the old
column and adding a new one, which would have thrown away every number already
recorded against a guest.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('guests', '0001_initial'),
    ]

    operations = [
        migrations.RenameField(
            model_name='guest',
            old_name='alt_mobile',
            new_name='whatsapp_number',
        ),
        migrations.AlterField(
            model_name='guest',
            name='whatsapp_number',
            field=models.CharField(
                blank=True,
                help_text='Leave empty to send WhatsApp to the mobile number above.',
                max_length=32,
                verbose_name='WhatsApp number',
            ),
        ),
    ]
