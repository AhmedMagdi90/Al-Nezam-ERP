from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0071_employeeshiftchangelog"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="workorder",
            name="material_readiness_status",
            field=models.CharField(
                choices=[
                    ("not_checked", "Not Checked"),
                    ("ready", "Ready"),
                    ("shortage", "Shortage"),
                ],
                default="not_checked",
                help_text="Planner-controlled manufacturing material readiness gate.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="material_shortage_note",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Planner note explaining any material shortage before production release.",
            ),
        ),
        migrations.AddField(
            model_name="workorder",
            name="material_readiness_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workorder",
            name="material_readiness_updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="material_readiness_updates",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="workorder",
            index=models.Index(
                fields=["company", "material_readiness_status"],
                name="mf_wo_mat_ready_idx",
            ),
        ),
    ]
