from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_alter_profile_department"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="planned_shift",
            field=models.CharField(blank=True, max_length=30, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="planned_shift_start_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
