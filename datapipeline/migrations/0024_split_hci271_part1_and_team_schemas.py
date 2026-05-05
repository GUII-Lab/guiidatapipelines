"""Split the HCI 271 Wk6 schema:

- form mode  -> Part 1 only  (1.1, 1.2, 1.3, 1.4)  — 4 personal-reflection sections
- group mode -> Parts 2 + 3  (2.1, 2.2, 2.3, 2.4, 3.1, 3.2) — 6 team-process sections

Per the PDF template Part 3 ("Open Question" / "Commitment for Next Week") is
phrased in team voice and bound to team decisions, so it belongs with the
in-group survey, not personal reflection. The In-Group flow now also enforces
coverage via the same engine (FormSchema), so students answer every section.
"""
from django.db import migrations


HCI271_WK6_FORM_BODY = {
    "schema_id": "hci271-week6-reflection",
    "version": "3.0.0",
    "title": "HCI 271 — Week 6 Personal Reflection (Part 1)",
    "course": "HCI 271",
    "instructor": "Magy Seif El-Nasr",
    "week": 6,
    "intro": (
        "I'll walk you through 4 personal-reflection prompts from this week's template "
        "(Part 1). Your team-process reflection (Parts 2 and 3) is a separate survey. "
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


HCI271_WK6_TEAM_BODY = {
    "schema_id": "hci271-week6-team-reflection",
    "version": "1.0.0",
    "title": "HCI 271 — Week 6 Team Reflection (Parts 2 & 3)",
    "course": "HCI 271",
    "instructor": "Magy Seif El-Nasr",
    "week": 6,
    "intro": (
        "I'll walk you through 6 team-process prompts from this week's template "
        "(Parts 2 and 3). This survey is the team-feedback companion to your "
        "personal reflection. You can ask to revise an earlier answer at any point. "
        "Type STOP to end early. When we finish, you'll get a file to upload to Canvas."
    ),
    "transition_template": "Thanks — anything else on {{section_topic}} before we move on?",
    "advance_template": "Got it. Now let's switch to {{next_section_title}} — {{next_section_one_line}}.",
    "shallow_word_threshold": 25,
    "max_probes_per_section": 1,
    "sections": [
        {
            "id": "2.1",
            "title": "Planning & Execution",
            "topic": "how your team planned and executed this week",
            "one_line": "your team's plan, division of work, and adaptation",
            "opening_prompt": "Did your team have a shared understanding of the week's goals before you started, or did that emerge as you went?",
            "depth_probe": "If you adapted the plan mid-week, what triggered the change and was it the right call in hindsight?",
            "fields": [{"id": "2.1", "kind": "longform"}],
        },
        {
            "id": "2.2",
            "title": "Roles & Contributions",
            "topic": "who did what on your team this week",
            "one_line": "list each teammate and their primary contribution this week",
            "opening_prompt": "Let's capture what each teammate worked on this week. Who's on your team — list everyone, including yourself?",
            "depth_probe": "Was the distribution of work equitable given each person's strengths? If not, what would you change?",
            "fields": [
                {"id": "2.2.roster", "kind": "table", "columns": ["Team Member", "Primary Role / Contribution This Week"], "min_rows": 2},
                {"id": "2.2.equity", "kind": "shortform", "label": "Was distribution equitable? If not, what would you change?"},
            ],
            "collection_strategy": "ask for the full roster up front, then walk member-by-member for contributions, then ask the equity question.",
        },
        {
            "id": "2.3",
            "title": "Collaboration & Communication",
            "topic": "how your team communicated and made decisions",
            "one_line": "what worked, what was challenging, one actionable improvement",
            "opening_prompt": "Tell me about one specific communication moment from this week that worked well — what exactly happened?",
            "depth_probe": "Now flip it — what specifically broke down? Avoid 'communication was hard' — give me the concrete moment and its impact.",
            "fields": [
                {"id": "2.3.worked", "kind": "shortform", "label": "What worked well (concrete)"},
                {"id": "2.3.challenge", "kind": "shortform", "label": "What was challenging (specific)"},
                {"id": "2.3.improvement", "kind": "shortform", "label": "One actionable improvement for next week (measurable)"},
            ],
        },
        {
            "id": "2.4",
            "title": "Team Health Check",
            "topic": "your 1–5 ratings on team functioning",
            "one_line": "five 1–5 ratings with brief justifications",
            "opening_prompt": "Now five quick team-health ratings on a 1–5 scale (1 = strongly disagree, 5 = strongly agree), each with a one-sentence why. Ready?",
            "depth_probe": None,
            "collection_strategy": "ask justification first, then rating, for each dimension — to prevent default-fives.",
            "fields": [
                {"id": "2.4.shared_goal",     "kind": "rating_with_justification", "dimension": "We had a clear, shared goal for the week."},
                {"id": "2.4.heard",           "kind": "rating_with_justification", "dimension": "Everyone's contributions were valued and heard."},
                {"id": "2.4.disagreements",   "kind": "rating_with_justification", "dimension": "We resolved disagreements constructively."},
                {"id": "2.4.commitments",     "kind": "rating_with_justification", "dimension": "We met our commitments and deadlines to each other."},
                {"id": "2.4.confidence",      "kind": "rating_with_justification", "dimension": "I feel confident about our direction going into next week."},
            ],
        },
        {
            "id": "3.1",
            "title": "Our Biggest Open Question",
            "topic": "the one question your team most needs to answer next",
            "one_line": "the open question (about users, problem space, methods, or team)",
            "opening_prompt": "What is the single question — about your users, your problem, your method, or your team — that your team most needs to answer before you can move forward with confidence?",
            "depth_probe": "Why that one specifically? What makes it the bottleneck right now?",
            "fields": [{"id": "3.1", "kind": "longform"}],
        },
        {
            "id": "3.2",
            "title": "Our Commitment for Next Week",
            "topic": "your team's concrete commitment for next week",
            "one_line": "one specific commitment your team is making",
            "opening_prompt": "Based on everything you just reflected on, what's one concrete commitment your team is making for next week — process change, research action, or design decision?",
            "depth_probe": "How will you know you actually did it? What's the observable signal?",
            "fields": [{"id": "3.2", "kind": "longform"}],
        },
    ],
    "closing": {
        "behavior": "After all sections have at least one student response and the student signals done, ask one feedback question about the bot itself, then emit [END] on its own line.",
        "feedback_prompt": "Last thing — did this team-process conversation surface more honest reflection than filling out the PDF would have, and what would make it work better next week?",
    },
    "ordering_rules": {
        "strict_in_order": True,
        "must_cover_all_sections": True,
        "stop_warn_then_honor": True,
    },
}


def upsert(apps, schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    for body in (HCI271_WK6_FORM_BODY, HCI271_WK6_TEAM_BODY):
        FormSchema.objects.update_or_create(
            schema_id=body["schema_id"],
            defaults={
                "version": body["version"],
                "title": body["title"],
                "course_label": body["course"],
                "week_number": body["week"],
                "body": body,
                "is_active": True,
            },
        )


def downgrade(apps, schema_editor):
    # Roll back the form schema to the v2 (Parts 1 & 3) shape would be lossy;
    # keep the rows in place. Only delete the new team-reflection row.
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    FormSchema.objects.filter(schema_id="hci271-week6-team-reflection").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("datapipeline", "0023_seed_hci271_form_schema"),
    ]
    operations = [migrations.RunPython(upsert, downgrade)]
