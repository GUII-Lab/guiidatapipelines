"""Fix the team-reflection schema's closing-feedback prompt.

The Wk6-derived schema body had its `closing.feedback_prompt` copied from the
form-mode (Part 1) survey: "did this team-process conversation surface more
honest reflection than filling out the PDF would have…". The Wk6 / Wk9 /
Wk10 in-group prompts in `LEAI/docs/prompts/wk*-hci271-group.md` all
specify a different closing line — directly asks whether the conversation
was useful, no PDF comparison. Update the stored body so the engine's
`dirClose` directive feeds the bot the correct wording.

Only the closing.feedback_prompt key changes; sections, fields, etc. are
untouched. Schemas affected: `hci271-week6-team-reflection`.
"""

from django.db import migrations


NEW_CLOSING_PROMPT = (
    "Last thing — was this team-process conversation useful, "
    "and what would make it work better next week?"
)

OLD_CLOSING_PROMPT = (
    "Last thing — did this team-process conversation surface more honest "
    "reflection than filling out the PDF would have, and what would make "
    "it work better next week?"
)

SCHEMA_ID = "hci271-week6-team-reflection"


def _swap_closing(FormSchema, target_prompt):
    try:
        row = FormSchema.objects.get(schema_id=SCHEMA_ID)
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
    _swap_closing(FormSchema, NEW_CLOSING_PROMPT)


def downgrade(apps, schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    _swap_closing(FormSchema, OLD_CLOSING_PROMPT)


class Migration(migrations.Migration):
    dependencies = [
        ("datapipeline", "0031_feedbackmessage_source_leaipdfingestbatch_and_more"),
    ]
    operations = [migrations.RunPython(upgrade, downgrade)]
