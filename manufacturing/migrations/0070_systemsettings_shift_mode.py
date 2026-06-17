from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0069_machine_shift_configuration_and_use_factory_shifts"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemsettings",
            name="shift_mode",
            field=models.CharField(
                choices=[("1", "1 Shift"), ("2", "2 Shifts"), ("3", "3 Shifts")],
                default="3",
                help_text="How many factory shifts are active per day.",
                max_length=1,
            ),
        ),
    ]
