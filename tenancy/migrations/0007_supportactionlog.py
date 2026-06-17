from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0006_organization_support_notes"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupportActionLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("actor_email", models.EmailField(blank=True, default="", max_length=254)),
                ("action_type", models.CharField(max_length=64)),
                ("target_label", models.CharField(blank=True, default="", max_length=255)),
                ("notes", models.TextField(blank=True, default="")),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="support_action_logs", to="tenancy.organization")),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
