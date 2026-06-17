from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0076_remove_workorder_draft_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bomoperation",
            name="setup_time",
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                help_text="Setup time in minutes (once per batch)",
                max_digits=10,
            ),
        ),
        migrations.AlterField(
            model_name="bomoperation",
            name="run_time",
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                help_text="Run time in minutes per single unit",
                max_digits=10,
            ),
        ),
    ]
