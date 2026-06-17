from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0068_systemsettings_default_operation_flow_mode_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="machine",
            name="shift_configuration",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Optional machine-level shift configuration with enabled flags and start/end times.",
            ),
        ),
        migrations.AddField(
            model_name="machine",
            name="use_factory_shifts",
            field=models.BooleanField(
                default=True,
                help_text="If enabled, this machine uses the company shift configuration for scheduling availability.",
            ),
        ),
    ]
