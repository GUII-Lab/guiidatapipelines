"""Drop the FeedbackGPT.survey_type column.

The original "individual / group" Session Type concept is being discarded
and will be redesigned from scratch in the future. Removing the column now
keeps the schema honest about what the application actually supports — a
later redesign can introduce its own field with whatever shape it needs.

Pre-existing FeedbackMessage rows whose session_id starts with ``group_``
(left over from previous group sessions) are *not* deleted by this
migration; they remain in the database as plain message rows that can be
inspected or removed manually if desired.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0017_hash_existing_course_passwords'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='feedbackgpt',
            name='survey_type',
        ),
    ]
