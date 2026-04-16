from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0019_leai_chat_and_quicktake'),
    ]

    operations = [
        migrations.AddField(
            model_name='leaiquicktake',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('running', 'Running'),
                    ('ready', 'Ready'),
                    ('failed', 'Failed'),
                ],
                default='ready',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='leaiquicktake',
            name='error',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='leaiquicktake',
            name='job_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
