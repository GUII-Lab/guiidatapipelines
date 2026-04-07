import secrets
import string

from django.db import migrations, models


def generate_unique_public_ids(apps, schema_editor):
    FeedbackGPT = apps.get_model('datapipeline', 'FeedbackGPT')
    alphabet = string.ascii_letters + string.digits
    used = set()
    for gpt in FeedbackGPT.objects.all():
        while True:
            candidate = ''.join(secrets.choice(alphabet) for _ in range(12))
            if candidate not in used:
                used.add(candidate)
                gpt.public_id = candidate
                gpt.save(update_fields=['public_id'])
                break


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0011_course_feedbackgpt_course_fields_feedbackmessage_gpt_id'),
    ]

    operations = [
        # Step 1: add column without unique constraint, nullable so existing rows can be blank
        migrations.AddField(
            model_name='feedbackgpt',
            name='public_id',
            field=models.CharField(blank=True, default='', max_length=16),
        ),
        # Step 2: backfill unique IDs for any existing rows
        migrations.RunPython(generate_unique_public_ids, migrations.RunPython.noop),
        # Step 3: now enforce uniqueness
        migrations.AlterField(
            model_name='feedbackgpt',
            name='public_id',
            field=models.CharField(blank=True, default='', max_length=16, unique=True),
        ),
    ]
