from django.db import migrations


PROFILE_TABLE = "accounts_profile"


def _profile_table_exists(connection):
    return PROFILE_TABLE in connection.introspection.table_names()


def forwards(apps, schema_editor):
    if schema_editor.connection.alias != "default":
        return
    if not _profile_table_exists(schema_editor.connection):
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {PROFILE_TABLE} SET app_scope = %s WHERE app_scope = %s",
            ["manufacturing", "planner"],
        )


def backwards(apps, schema_editor):
    if schema_editor.connection.alias != "default":
        return
    if not _profile_table_exists(schema_editor.connection):
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {PROFILE_TABLE} SET app_scope = %s WHERE app_scope = %s",
            ["planner", "manufacturing"],
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0013_convert_planner_app_scope"),
        ("tenancy", "0007_supportactionlog"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
