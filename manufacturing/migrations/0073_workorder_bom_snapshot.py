from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0072_workorder_material_readiness"),
    ]

    operations = [
        migrations.AddField(
            model_name="workorder",
            name="bom_version",
            field=models.CharField(
                blank=True,
                default="",
                help_text="BOM version captured when this work order was created.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="bom_snapshot",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Immutable BOM structure captured when this work order was created.",
            ),
        ),
    ]
