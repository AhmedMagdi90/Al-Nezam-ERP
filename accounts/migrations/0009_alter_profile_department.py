from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_profile_worker_mode_enabled"),
    ]

    operations = [
        migrations.AlterField(
            model_name="profile",
            name="department",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
    ]
