from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0073_workorder_bom_snapshot"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="workorder",
            name="bom_change_status",
            field=models.CharField(
                choices=[
                    ("none", "No BOM Change"),
                    ("action_required", "Action Required"),
                    ("latest_applied", "Latest BOM Applied"),
                    ("archived_replaced", "Archived and Replaced"),
                    ("scrap_applied", "Scrap Done and Latest BOM Applied"),
                    ("ignored", "Continue Old BOM"),
                ],
                default="none",
                help_text="Planner decision state when a newer active BOM affects this work order.",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="bom_change_latest_bom",
            field=models.ForeignKey(
                blank=True,
                help_text="Latest active BOM version that triggered the action-required warning.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="impacted_work_orders",
                to="manufacturing.billofmaterial",
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="bom_change_detected_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workorder",
            name="bom_change_decision_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workorder",
            name="bom_change_decision_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="bom_change_decisions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="bom_change_decision_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="workorder",
            name="bom_change_replacement_wo",
            field=models.ForeignKey(
                blank=True,
                help_text="Replacement work order created from the latest BOM.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="bom_change_replaced_sources",
                to="manufacturing.workorder",
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="bom_change_scrapped_qty",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Finished/reported quantity treated as scrapped when applying the new BOM.",
            ),
        ),
        migrations.AddIndex(
            model_name="workorder",
            index=models.Index(fields=["company", "bom_change_status"], name="mf_wo_bom_change_idx"),
        ),
    ]
