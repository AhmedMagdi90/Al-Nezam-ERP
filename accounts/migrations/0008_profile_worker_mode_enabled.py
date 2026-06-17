from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_profile_shift"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="worker_mode_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
