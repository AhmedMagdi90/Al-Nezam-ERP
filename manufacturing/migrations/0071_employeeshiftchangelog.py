from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("manufacturing", "0070_systemsettings_shift_mode"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmployeeShiftChangeLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("assign", "Assign shift"),
                            ("swap", "Swap shift"),
                            ("clear", "Clear planned shift"),
                        ],
                        max_length=20,
                    ),
                ),
                ("previous_shift", models.CharField(blank=True, max_length=30, null=True)),
                ("new_shift", models.CharField(blank=True, max_length=30, null=True)),
                ("previous_planned_shift", models.CharField(blank=True, max_length=30, null=True)),
                ("previous_planned_shift_start_date", models.DateField(blank=True, null=True)),
                ("effective_start_date", models.DateField(blank=True, null=True)),
                ("note", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="employee_shift_changes_made",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="employee_shift_change_logs",
                        to="manufacturing.company",
                    ),
                ),
                (
                    "employee",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="employee_shift_change_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="employeeshiftchangelog",
            index=models.Index(fields=["company", "employee", "created_at"], name="mf_emp_shift_log_idx"),
        ),
    ]
