from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0014_feedbackgpt_canvas_integration'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='feedbackgpt',
            name='themes',
        ),
        migrations.RemoveField(
            model_name='feedbackgpt',
            name='timing_category',
        ),
    ]
