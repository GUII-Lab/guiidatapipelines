from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0013_feedbackgpt_anonymity_mode_feedbackgpt_expires_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='feedbackgpt',
            name='canvas_integration',
            field=models.BooleanField(default=False),
        ),
    ]
