from django.db import migrations


def forwards(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    db_alias = schema_editor.connection.alias
    Profile.objects.using(db_alias).filter(app_scope="planner").update(
        app_scope="manufacturing"
    )


def backwards(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    db_alias = schema_editor.connection.alias
    Profile.objects.using(db_alias).filter(app_scope="manufacturing").update(
        app_scope="planner"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_profile_app_scope_manufacturing"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
