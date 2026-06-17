from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manufacturing', '0058_qualitycheck_approved_at_qualitycheck_approved_by_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='workorder',
            name='planner_start_at',
            field=models.DateTimeField(blank=True, help_text='When planner scheduled the WO', null=True),
        ),
        migrations.AddField(
            model_name='workorder',
            name='supervisor_start_at',
            field=models.DateTimeField(blank=True, help_text='When supervisor assigned a worker', null=True),
        ),
        migrations.AddField(
            model_name='workorder',
            name='worker_start_at',
            field=models.DateTimeField(blank=True, help_text='When worker started production', null=True),
        ),
        migrations.AddField(
            model_name='workorder',
            name='quality_start_at',
            field=models.DateTimeField(blank=True, help_text='When quality check started', null=True),
        ),
    ]

