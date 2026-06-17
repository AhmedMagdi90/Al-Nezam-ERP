from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manufacturing', '0067_bomoperationmaterial'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemsettings',
            name='default_operation_flow_mode',
            field=models.CharField(
                choices=[('series', 'Series'), ('parallel', 'Parallel')],
                default='series',
                help_text='Default execution flow for work order stages.',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='workorder',
            name='operation_flow_mode',
            field=models.CharField(
                choices=[('series', 'Series'), ('parallel', 'Parallel')],
                default='series',
                help_text='Whether BOM stages run in series or in parallel for this work order.',
                max_length=20,
            ),
        ),
    ]
