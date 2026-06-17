from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Tenant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200)),
                (
                    "code",
                    models.SlugField(
                        help_text="Stable tenant key, e.g. al-nour",
                        max_length=64,
                        unique=True,
                    ),
                ),
                (
                    "db_alias",
                    models.CharField(
                        help_text="Django DB alias, e.g. tenant_al_nour",
                        max_length=64,
                        unique=True,
                    ),
                ),
                (
                    "db_name",
                    models.CharField(
                        help_text="SQLite file path (relative to BASE_DIR or absolute), e.g. tenant_dbs/al_nour.sqlite3",
                        max_length=255,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"]},
        ),
    ]

