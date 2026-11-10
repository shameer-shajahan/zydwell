"""Insert Apartment between Floor and Room.

Building → Floor → Apartment → Room → Bed.

Written by hand: the auto-detector wanted to drop Room.floor and
Floor.unit_label before anything had filled the new level in, which would have
cut every room loose from its floor and thrown away the unit labels already in
use. Here each floor gets an apartment first — named from its unit_label where
one was set — and the rooms move across before the old columns go.
"""
import django.db.models.deletion
from django.db import migrations, models


def build_apartments(apps, schema_editor):
    Floor = apps.get_model('properties', 'Floor')
    Apartment = apps.get_model('properties', 'Apartment')
    Room = apps.get_model('properties', 'Room')

    for floor in Floor.objects.all():
        rooms = Room.objects.filter(floor=floor)
        if not rooms.exists() and not floor.unit_label:
            continue
        apartment = Apartment.objects.create(
            tenant_id=floor.tenant_id,
            floor=floor,
            # The unit_label was already being used as the flat number.
            name=floor.unit_label.strip() or '1',
            sort_order=0,
            is_archived=floor.is_archived,
        )
        rooms.update(apartment=apartment)


def unbuild_apartments(apps, schema_editor):
    Room = apps.get_model('properties', 'Room')
    for room in Room.objects.select_related('apartment').exclude(apartment__isnull=True):
        room.floor_id = room.apartment.floor_id
        room.save(update_fields=['floor'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
        ('properties', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Apartment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=60,
                                          verbose_name='apartment name or number')),
                ('notes', models.CharField(blank=True, max_length=160)),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('is_archived', models.BooleanField(default=False)),
                ('floor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name='apartments',
                                            to='properties.floor')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name='%(class)s_set',
                                             to='core.tenant')),
            ],
            options={'ordering': ['floor__building__name', 'floor__sort_order',
                                  'sort_order', 'name']},
        ),
        migrations.AddField(
            model_name='room',
            name='apartment',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name='rooms', to='properties.apartment'),
        ),

        migrations.RunPython(build_apartments, unbuild_apartments),

        migrations.AlterField(
            model_name='room',
            name='apartment',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                    related_name='rooms', to='properties.apartment'),
        ),
        migrations.RemoveConstraint(model_name='room', name='uniq_room_number_per_floor'),
        migrations.RemoveField(model_name='room', name='floor'),
        migrations.RemoveField(model_name='floor', name='unit_label'),

        migrations.AlterModelOptions(
            name='room',
            options={'ordering': ['apartment__floor__building__name',
                                  'apartment__floor__sort_order',
                                  'apartment__sort_order', 'number']},
        ),
        migrations.AlterModelOptions(
            name='bed',
            options={'ordering': ['room__apartment__floor__sort_order',
                                  'room__apartment__sort_order', 'room__number', 'label']},
        ),
        migrations.AddConstraint(
            model_name='apartment',
            constraint=models.UniqueConstraint(fields=('floor', 'name'),
                                               name='uniq_apartment_per_floor'),
        ),
        migrations.AddConstraint(
            model_name='room',
            constraint=models.UniqueConstraint(fields=('apartment', 'number'),
                                               name='uniq_room_number_per_apartment'),
        ),
    ]
