from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0062_bomcomponent_product"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemsettings",
            name="department_catalog",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Custom departments grouped by app scope e.g. {'planner': ['Machining', 'Stores']}",
            ),
        ),
    ]
