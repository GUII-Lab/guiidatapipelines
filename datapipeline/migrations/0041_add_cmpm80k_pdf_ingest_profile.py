"""Add deterministic PDF cleanup metadata to the CMPM 80K individual schema."""

from copy import deepcopy

from django.db import migrations


SCHEMA_ID = "cmpm80k-reflection"

PDF_INGEST_PROFILE = {
    "section_instruction_prefixes": {
        "1.1": [
            "What was the single most important concept, framework, or skill introduced or reinforced this "
            "week? Describe it in your own words rather than restating the dispatch. Explain why it stood out "
            "to you and how you currently understand it."
        ],
        "1.2": [
            "Describe the main thing you made or played this week. Address the following:\n"
            "How did you go about it?\n"
            "What did the process feel like in practice versus how it was described?\n"
            "What were the results, and what did you learn from actually doing it?"
        ],
        "1.3": [
            "Reflect on the gap between your prior understanding and your current one. Surfacing what you "
            "assumed, what changed, and what still feels uncertain is one of the most valuable exercises in "
            "any learning process."
        ],
    },
    "stop_headings": ["Submission Guidelines", "Raw Conversation Transcript"],
    "suspicious_markers": ["Submission Guidelines", "Raw Conversation Transcript"],
    "answer_soft_max_chars": 2000,
}


def add_profile(apps, _schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    try:
        schema = FormSchema.objects.get(schema_id=SCHEMA_ID)
    except FormSchema.DoesNotExist:
        return
    body = dict(schema.body or {})
    body["pdf_ingest"] = deepcopy(PDF_INGEST_PROFILE)
    schema.body = body
    schema.save(update_fields=["body"])


def remove_profile(apps, _schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    try:
        schema = FormSchema.objects.get(schema_id=SCHEMA_ID)
    except FormSchema.DoesNotExist:
        return
    body = dict(schema.body or {})
    body.pop("pdf_ingest", None)
    schema.body = body
    schema.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("datapipeline", "0040_course_identity_tracking_enabled_sessionidentity")]

    operations = [migrations.RunPython(add_profile, remove_profile)]
