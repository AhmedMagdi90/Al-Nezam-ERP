from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_profile_planned_shift"),
    ]

    operations = [
        migrations.AlterField(
            model_name="profile",
            name="app_scope",
            field=models.CharField(
                choices=[
                    ("planner", "Planner"),
                    ("store", "Store"),
                    ("quality", "Quality"),
                    ("maintenance", "Maintenance"),
                ],
                default="planner",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="role",
            name="name",
            field=models.CharField(
                choices=[
                    ("admin", "Admin"),
                    ("planner", "Planner"),
                    ("supervisor", "Supervisor"),
                    ("worker", "Worker"),
                    ("store", "Store"),
                    ("quality", "Quality Officer"),
                    ("maintenance", "Maintenance"),
                ],
                max_length=50,
                unique=True,
            ),
        ),
    ]
