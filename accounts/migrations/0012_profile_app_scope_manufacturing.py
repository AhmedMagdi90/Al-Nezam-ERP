from django.db import migrations, models


def forwards(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    Profile.objects.filter(app_scope="planner").update(app_scope="manufacturing")


def backwards(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    Profile.objects.filter(app_scope="manufacturing").update(app_scope="planner")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0011_alter_profile_app_scope_alter_role_name"),
    ]

    operations = [
        migrations.AlterField(
            model_name="profile",
            name="app_scope",
            field=models.CharField(
                choices=[
                    ("manufacturing", "Manufacturing"),
                    ("store", "Store"),
                    ("quality", "Quality"),
                    ("maintenance", "Maintenance"),
                ],
                default="manufacturing",
                max_length=20,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
