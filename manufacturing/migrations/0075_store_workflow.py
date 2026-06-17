from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0074_workorder_bom_change_impact"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="workorder",
            name="material_readiness_status",
            field=models.CharField(
                choices=[
                    ("not_checked", "Not Checked"),
                    ("ready", "Ready"),
                    ("partial", "Partially Ready"),
                    ("shortage", "Shortage"),
                ],
                default="not_checked",
                help_text="Planner-controlled manufacturing material readiness gate.",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="workorder",
            name="material_shortage_note",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Store note explaining material availability before production release.",
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="material_available_qty",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Available production quantity confirmed by store for partial BOM readiness.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="store_receipt_status",
            field=models.CharField(
                choices=[
                    ("not_requested", "Not Requested"),
                    ("pending", "Pending Store Receipt"),
                    ("received", "Received by Store"),
                ],
                default="not_requested",
                help_text="Finished-goods receipt gate before planner can close the WO.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="store_receipt_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workorder",
            name="store_receipt_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workorder",
            name="store_receipt_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="store_receipt_confirmations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="store_received_qty",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workorder",
            name="store_scrap_qty",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workorder",
            name="store_receipt_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddIndex(
            model_name="workorder",
            index=models.Index(fields=["company", "store_receipt_status"], name="mf_wo_store_receipt_idx"),
        ),
    ]
