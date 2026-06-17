from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0063_systemsettings_department_catalog"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="workorder",
            index=models.Index(
                fields=["company", "start_date"],
                name="mf_wo_company_start_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="workorder",
            index=models.Index(
                fields=["company", "bom"],
                name="mf_wo_company_bom_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="productionlog",
            index=models.Index(
                fields=["work_order", "date"],
                name="mf_log_wo_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="productionlog",
            index=models.Index(
                fields=["work_order", "created_at"],
                name="mf_log_wo_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="materialusage",
            index=models.Index(
                fields=["production_log", "product"],
                name="mf_usage_log_prod_idx",
            ),
        ),
    ]
