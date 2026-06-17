from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manufacturing', '0064_reporting_indexes'),
    ]

    operations = [
        migrations.AlterField(
            model_name='product',
            name='name',
            field=models.CharField(max_length=100),
        ),
        migrations.AddConstraint(
            model_name='product',
            constraint=models.UniqueConstraint(fields=('company', 'name'), name='uniq_product_company_name'),
        ),
    ]
