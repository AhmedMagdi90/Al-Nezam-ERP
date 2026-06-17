from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manufacturing', '0056_workorder_change_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='machine',
            name='image',
            field=models.ImageField(blank=True, null=True, upload_to='machines/'),
        ),
    ]
