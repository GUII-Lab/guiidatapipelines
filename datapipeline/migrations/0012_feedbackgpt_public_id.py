from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0011_course_feedbackgpt_course_fields_feedbackmessage_gpt_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='feedbackgpt',
            name='public_id',
            field=models.CharField(blank=True, default='', max_length=16, unique=True),
        ),
    ]
