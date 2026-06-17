from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0078_material_readiness_percent_delivery"),
    ]

    operations = [
        migrations.AddField(
            model_name="materialusage",
            name="planned_quantity",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True),
        ),
    ]
