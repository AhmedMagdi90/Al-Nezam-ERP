import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0080_bom_attachment"),
    ]

    operations = [
        migrations.AddField(
            model_name="workorder",
            name="source_bom_component",
            field=models.ForeignKey(
                blank=True,
                help_text="BOM component shortage that generated this sub-assembly work order.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="generated_work_orders",
                to="manufacturing.bomcomponent",
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="subassembly_parent",
            field=models.ForeignKey(
                blank=True,
                help_text="Parent work order that requires this manufactured sub-assembly.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="subassembly_work_orders",
                to="manufacturing.workorder",
            ),
        ),
    ]
