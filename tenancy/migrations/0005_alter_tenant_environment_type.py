from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0004_platformsettings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="environment_type",
            field=models.CharField(
                choices=[
                    ("demo", "Demo"),
                    ("live", "Live"),
                    ("test", "Test"),
                    ("dev", "Dev"),
                ],
                default="live",
                max_length=16,
            ),
        ),
    ]
