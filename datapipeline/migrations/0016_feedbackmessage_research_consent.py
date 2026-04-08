from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0015_remove_feedbackgpt_themes_timing_category'),
    ]

    operations = [
        migrations.AddField(
            model_name='feedbackmessage',
            name='research_consent',
            field=models.BooleanField(default=False),
        ),
    ]
