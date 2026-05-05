"""Seed the HCI 271 Week 6 form schema (Parts 1 + 3 only).

Part 2 of the original PDF reflection template (team-process — Planning,
Roles, Collaboration, Health Check) is now handled by a separate In-Group
survey instead of being part of the form schema. This split was decided
post-Tuesday demo so each mode owns its natural genre:
  - form  -> personal reflection (Parts 1, 3)
  - group -> peer / team-process feedback (Part 2)

Edits after this migration should happen through the Django admin so we
don't keep redeploying for content tweaks.
"""
from django.db import migrations


HCI271_WK6_FORM_BODY = {
    "schema_id": "hci271-week6-reflection",
    "version": "2.0.0",
    "title": "HCI 271 — Week 6 Personal Reflection (Parts 1 & 3)",
    "course": "HCI 271",
    "instructor": "Magy Seif El-Nasr",
    "week": 6,
    "intro": (
        "I'll walk you through 6 personal-reflection prompts from this week's template "
        "(Parts 1 and 3). Your team-process reflection (Part 2) is a separate survey. "
        "You can ask to revise an earlier answer at any point. Type STOP to end early. "
        "When we finish, you'll get a file to upload to Canvas."
    ),
    "transition_template": "Thanks — anything else on {{section_topic}} before we move on?",
    "advance_template": "Got it. Now let's switch to {{next_section_title}} — {{next_section_one_line}}.",
    "shallow_word_threshold": 25,
    "max_probes_per_section": 1,
    "sections": [
        {
            "id": "1.1",
            "title": "Key Concepts & Takeaways",
            "topic": "the concept that stuck with you this week",
            "one_line": "the single concept or skill from Week 6 that stood out",
            "opening_prompt": "What's one idea from this week's data analysis session that actually stuck with you, and why?",
            "depth_probe": "Can you anchor that in something concrete — a moment in lecture, a slide, a sticky note, or a piece of your team's data — that made it click?",
            "fields": [{"id": "1.1", "kind": "longform", "target_words": "2–3 paragraphs"}],
        },
        {
            "id": "1.2",
            "title": "Methods in Practice",
            "topic": "the synthesis method you actually applied this week",
            "one_line": "a specific synthesis method you used (affinity diagramming, thematic coding, journey mapping, etc.)",
            "opening_prompt": "Which synthesis method did you actually try with your team this week, and how did it feel in practice versus how it was described in lecture?",
            "depth_probe": "Did you hit any of the NN/g pitfalls — clustering by topic instead of behavior, premature labelling, one person driving the wall, going abstract too fast? Pick one and tell me what happened.",
            "fields": [
                {
                    "id": "1.2",
                    "kind": "longform",
                    "target_words": "2–3 paragraphs",
                    "subprompts": ["how you applied it", "practice vs theory", "results and learning"],
                }
            ],
        },
        {
            "id": "1.3",
            "title": "Knowledge Shift: Before vs. After",
            "topic": "how your understanding changed this week",
            "one_line": "what you thought before, what surprised you, what's still uncertain",
            "opening_prompt": "Before this week, what did you assume about turning raw data into design direction?",
            "depth_probe": "What specifically challenged that assumption — a piece of evidence, a teammate's point, a moment in the affinity wall?",
            "fields": [
                {"id": "1.3a", "kind": "shortform", "label": "What I thought I knew"},
                {"id": "1.3b", "kind": "shortform", "label": "What I was surprised by"},
                {"id": "1.3c", "kind": "shortform", "label": "What I am still uncertain about"},
            ],
        },
        {
            "id": "1.4",
            "title": "Connection to the Capstone Project",
            "topic": "how this week's learning lands in your team's project",
            "one_line": "one specific way this week's learning will change your project going forward",
            "opening_prompt": "How will what you learned this week change your team's research approach, your interview questions, or an emerging design decision?",
            "depth_probe": "What's the smallest concrete change you'd make next week as a result?",
            "fields": [{"id": "1.4", "kind": "longform"}],
        },
        {
            "id": "3.1",
            "title": "Our Biggest Open Question",
            "topic": "the one question you most need to answer next",
            "one_line": "the open question (about users, problem space, methods, or team)",
            "opening_prompt": "What is the single question — about your users, your problem, your method, or your team — that you most need to answer before you can move forward with confidence?",
            "depth_probe": "Why that one specifically? What makes it the bottleneck right now?",
            "fields": [{"id": "3.1", "kind": "longform"}],
        },
        {
            "id": "3.2",
            "title": "Our Commitment for Next Week",
            "topic": "your concrete commitment for next week",
            "one_line": "one specific commitment your team is making",
            "opening_prompt": "Based on everything you just reflected on, what's one concrete commitment your team is making for next week — process change, research action, or design decision?",
            "depth_probe": "How will you know you actually did it? What's the observable signal?",
            "fields": [{"id": "3.2", "kind": "longform"}],
        },
    ],
    "closing": {
        "behavior": "After all sections have at least one student response and the student signals done, ask one feedback question about the bot itself, then emit [END] on its own line.",
        "feedback_prompt": "Last thing — did this conversation surface more honest reflection than filling out the PDF would have, and what would make it work better next week?",
    },
    "ordering_rules": {
        "strict_in_order": True,
        "must_cover_all_sections": True,
        "stop_warn_then_honor": True,
    },
}


def seed(apps, schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    FormSchema.objects.update_or_create(
        schema_id=HCI271_WK6_FORM_BODY["schema_id"],
        defaults={
            "version": HCI271_WK6_FORM_BODY["version"],
            "title": HCI271_WK6_FORM_BODY["title"],
            "course_label": HCI271_WK6_FORM_BODY["course"],
            "week_number": HCI271_WK6_FORM_BODY["week"],
            "body": HCI271_WK6_FORM_BODY,
            "is_active": True,
        },
    )


def unseed(apps, schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    FormSchema.objects.filter(schema_id=HCI271_WK6_FORM_BODY["schema_id"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("datapipeline", "0022_formschema_alter_feedbackgpt_mode_and_more"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
