from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0051_workorder_released_qty"),
    ]

    operations = [
        migrations.AddField(
            model_name="workorder",
            name="qc_requirement",
            field=models.BooleanField(default=False, help_text="If true, this stage requires QC approval before releasing to the next stage."),
        ),
        migrations.AddField(
            model_name="workorder",
            name="closed_by_planner",
            field=models.BooleanField(default=False, help_text="Planner has already closed this work order."),
        ),
        migrations.AddField(
            model_name="workorder",
            name="source_task",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="released_children",
                to="manufacturing.workorder",
                help_text="Original stage task that released this task (split/QC tracking).",
            ),
        ),
    ]

