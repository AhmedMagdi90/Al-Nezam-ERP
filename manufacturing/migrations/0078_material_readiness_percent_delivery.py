from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0077_bom_operation_time_precision"),
    ]

    operations = [
        migrations.AddField(
            model_name="workorder",
            name="material_available_percent",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Material availability percentage confirmed by store.",
                max_digits=5,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="material_expected_delivery_date",
            field=models.DateField(
                blank=True,
                help_text="Expected material delivery date for partial or unavailable material.",
                null=True,
            ),
        ),
    ]
