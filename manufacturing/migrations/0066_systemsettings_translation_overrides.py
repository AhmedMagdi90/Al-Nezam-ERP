from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0065_product_company_name_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemsettings",
            name="translation_overrides",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Per-language company translation overrides e.g. {'ar': {'Actual vs Planned': '...'}}",
            ),
        ),
    ]
