from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0005_alter_tenant_environment_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="support_notes",
            field=models.TextField(blank=True, default=""),
        ),
    ]
