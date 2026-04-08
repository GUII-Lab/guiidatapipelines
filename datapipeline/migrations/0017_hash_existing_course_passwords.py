"""Hash any existing plaintext course passwords in place.

Up to migration 0016, ``Course.password`` was stored as plain text. Starting
with this migration the application uses ``django.contrib.auth.hashers`` to
hash on write and verify on read. This data migration walks every existing
``Course`` row and, for any password value that is not already a recognized
Django hash, replaces it with the hashed equivalent. Already-hashed values
are skipped, so this migration is idempotent.
"""

from django.contrib.auth.hashers import identify_hasher, make_password
from django.db import migrations


def hash_plaintext_passwords(apps, schema_editor):
    Course = apps.get_model('datapipeline', 'Course')
    for course in Course.objects.all():
        try:
            identify_hasher(course.password)
        except (ValueError, TypeError):
            # Not a recognized hash format -> treat as legacy plaintext.
            course.password = make_password(course.password or '')
            course.save(update_fields=['password'])


def noop_reverse(apps, schema_editor):
    """Reversing is not possible (we cannot recover the original plaintext)."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('datapipeline', '0016_feedbackmessage_research_consent'),
    ]

    operations = [
        migrations.RunPython(hash_plaintext_passwords, noop_reverse),
    ]
