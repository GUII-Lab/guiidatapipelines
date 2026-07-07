"""Fix the CMPM 80H schemas' closing-feedback prompts.

Both the individual (`cmpm80h-reflection`) and team-process
(`cmpm80h-team-reflection`) schema bodies had their `closing.feedback_prompt`
worded in a way that plants the words "honest" and "PDF" (individual) or
implies a yes/no framing (group) — biasing students toward affirming a
comparison the researcher is trying to measure (P0-4, CMPM 80H feedback
root-cause analysis). Update the stored bodies to the new, neutral wording so
the engine's `dirClose` directive feeds the bot the correct closing question.

Only the closing.feedback_prompt key changes; sections, fields, etc. are
untouched. Schemas affected: `cmpm80h-reflection`, `cmpm80h-team-reflection`.
Does NOT touch any hci271 schema.
"""

from django.db import migrations


NEW_INDIVIDUAL_PROMPT = (
    "Last thing — how did reflecting through this conversation compare to "
    "writing your reflection on your own, and what would make it better "
    "next time?"
)

OLD_INDIVIDUAL_PROMPT = (
    "Last thing — did this conversation surface more honest reflection "
    "than filling out the PDF would have, and what would make it work "
    "better next time?"
)

NEW_GROUP_PROMPT = (
    "Last thing — how did talking through your team's process this way "
    "work for you, and what would make it better next time?"
)

OLD_GROUP_PROMPT = (
    "Last thing — was this team-process conversation useful, and what "
    "would make it work better next time?"
)

SCHEMA_PROMPTS = {
    "cmpm80h-reflection": (OLD_INDIVIDUAL_PROMPT, NEW_INDIVIDUAL_PROMPT),
    "cmpm80h-team-reflection": (OLD_GROUP_PROMPT, NEW_GROUP_PROMPT),
}


def _swap_closing(FormSchema, schema_id, target_prompt):
    try:
        row = FormSchema.objects.get(schema_id=schema_id)
    except FormSchema.DoesNotExist:
        return
    body = dict(row.body or {})
    closing = dict(body.get("closing") or {})
    closing["feedback_prompt"] = target_prompt
    body["closing"] = closing
    row.body = body
    row.save(update_fields=["body"])


def upgrade(apps, schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    for schema_id, (_old, new) in SCHEMA_PROMPTS.items():
        _swap_closing(FormSchema, schema_id, new)


def downgrade(apps, schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    for schema_id, (old, _new) in SCHEMA_PROMPTS.items():
        _swap_closing(FormSchema, schema_id, old)


class Migration(migrations.Migration):
    dependencies = [
        ("datapipeline", "0036_course_bot_display_name"),
    ]
    operations = [migrations.RunPython(upgrade, downgrade)]
