from django.db import migrations, models


def migrate_work_order_drafts_to_pending(apps, schema_editor):
    WorkOrder = apps.get_model("manufacturing", "WorkOrder")
    WorkOrder.objects.filter(status="draft").update(status="pending")


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0075_store_workflow"),
    ]

    operations = [
        migrations.RunPython(migrate_work_order_drafts_to_pending, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="workorder",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending (Scheduled)"),
                    ("in_progress", "In Progress"),
                    ("completed", "Completed"),
                    ("hold", "On Hold"),
                    ("canceled", "Canceled"),
                    ("archived", "Archived"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
