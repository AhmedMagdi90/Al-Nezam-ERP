from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0066_systemsettings_translation_overrides"),
    ]

    operations = [
        migrations.CreateModel(
            name="BOMOperationMaterial",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "component",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="operation_links",
                        to="manufacturing.bomcomponent",
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="material_links",
                        to="manufacturing.bomoperation",
                    ),
                ),
            ],
            options={},
        ),
        migrations.AddConstraint(
            model_name="bomoperationmaterial",
            constraint=models.UniqueConstraint(
                fields=("operation", "component"),
                name="uniq_bom_operation_component",
            ),
        ),
    ]
