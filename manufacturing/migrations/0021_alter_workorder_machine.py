from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('manufacturing', '0020_bomoperation'),
    ]

    operations = [
        migrations.AlterField(
            model_name='workorder',
            name='machine',
            field=models.ForeignKey(
                blank=True,
                help_text='Optional: Assign to specific machine, or use BOM operations',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='manufacturing.machine'
            ),
        ),
    ]
