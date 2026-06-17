from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manufacturing', '0079_materialusage_planned_quantity'),
    ]

    operations = [
        migrations.AddField(
            model_name='billofmaterial',
            name='attachment',
            field=models.FileField(blank=True, null=True, upload_to='bom_attachments/'),
        ),
        migrations.AddField(
            model_name='billofmaterial',
            name='attachment_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
