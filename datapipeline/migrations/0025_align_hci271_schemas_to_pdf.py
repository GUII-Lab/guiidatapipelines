"""Align HCI 271 Week 6 schemas to the canonical Reflection_template.pdf.

Background:
    Migration 0024 split the Week 6 reflection into two schemas:
    - hci271-week6-reflection         (Part 1, individual / form-mode)
    - hci271-week6-team-reflection    (Parts 2 & 3, team / in-group)

    The bodies introduced there carried over instructor-specific framing that
    did not exist in the source PDF (e.g. references to "the data analysis
    session", "raw data into design direction", and the "NN/g pitfalls" probe
    in 1.2). That phrasing was injecting Week-6-lecture content into prompts
    the PDF expects to be neutral / template-driven.

    This migration replaces the body of both rows with prompts that come
    *only* from the Reflection_template.pdf wording — every opening_prompt,
    depth_probe, sub-field label, table column, and rating dimension is
    verbatim or a tight paraphrase of the PDF, with no extra material.

Per-section provenance is documented in
``LEAI/docs/instructor-clarifications/wk6-form-mode-SPEC.md``.

Downgrade restores the 0024 bodies (kept inline below) so this migration is
fully reversible.
"""
from django.db import migrations


# ─── PDF-aligned bodies (forward) ────────────────────────────────────────────

HCI271_WK6_FORM_BODY = {
    "schema_id": "hci271-week6-reflection",
    "version": "4.0.0",
    "title": "HCI 271 — Weekly Reflection Journal (Part 1: Individual)",
    "course": "HCI 271 — Capstone I",
    "instructor": "Magy Seif El-Nasr",
    "week": 6,
    "intro": (
        "I'll walk you through 4 personal-reflection prompts from Part 1 of the "
        "Weekly Reflection Journal template (Individual Learning & Discovery). "
        "Your team-process reflection (Parts 2 and 3) is a separate survey. "
        "You can ask to revise an earlier answer at any point. Type STOP to end early. "
        "When we finish, you'll get a file to upload to the course portal."
    ),
    "parts_blurb": "from Part 1 of the Weekly Reflection Journal template",
    "transition_template": "Thanks — anything else on {{section_topic}} before we move on?",
    "advance_template": "Got it. Now let's switch to {{next_section_title}} — {{next_section_one_line}}.",
    "shallow_word_threshold": 25,
    "max_probes_per_section": 1,
    "sections": [
        {
            "id": "1.1",
            "title": "Key Concepts & Takeaways",
            "topic": "the single most important concept, framework, or skill from this week",
            "one_line": "the single most important concept, framework, or skill introduced or reinforced this week",
            "opening_prompt": (
                "What was the single most important concept, framework, or skill introduced "
                "or reinforced this week? Describe it in your own words — avoid simply "
                "restating the lecture definition."
            ),
            "depth_probe": "Why did it stand out to you, and how do you currently understand it?",
            "fields": [{"id": "1.1", "kind": "longform", "target_words": "2–3 paragraphs"}],
        },
        {
            "id": "1.2",
            "title": "Methods in Practice",
            "topic": "a specific method or technique you applied this week",
            "one_line": "a specific method or technique you applied (e.g., contextual inquiry, affinity diagramming, journey mapping, research question brainstorming, laddering)",
            "opening_prompt": (
                "Describe a specific method or technique you applied this week (e.g., "
                "contextual inquiry, affinity diagramming, journey mapping, research "
                "question brainstorming, laddering). How did you apply it in the context "
                "of your capstone project?"
            ),
            "depth_probe": (
                "What did the process feel like in practice versus how it was described "
                "in theory, and what were the results — what did you learn from the "
                "experience of actually doing it?"
            ),
            "fields": [
                {
                    "id": "1.2",
                    "kind": "longform",
                    "target_words": "2–3 paragraphs",
                    "subprompts": [
                        "how you applied it in your capstone project",
                        "practice vs. theory",
                        "results and what you learned from doing it",
                    ],
                }
            ],
        },
        {
            "id": "1.3",
            "title": "Knowledge Shift: Before vs. After",
            "topic": "the gap between your prior understanding and your current one",
            "one_line": "what you thought you knew, what surprised you, what you are still uncertain about",
            "opening_prompt": (
                "Reflect on the gap between your prior understanding and your current one. "
                "First: what did you think you knew — your prior understanding, assumption, "
                "or mental model before this week's material or activity?"
            ),
            "depth_probe": (
                "Now: what insight, discovery, or piece of evidence challenged or shifted "
                "your thinking — be specific about what exactly surprised you and why? "
                "And finally, what's one concept, method, or aspect of the project that "
                "you do not yet fully understand?"
            ),
            "fields": [
                {"id": "1.3a", "kind": "shortform", "label": "What I thought I knew"},
                {"id": "1.3b", "kind": "shortform", "label": "What I was surprised by"},
                {"id": "1.3c", "kind": "shortform", "label": "What I am still uncertain about"},
            ],
        },
        {
            "id": "1.4",
            "title": "Connection to the Capstone Project",
            "topic": "how this week's learning connects to your team's capstone project",
            "one_line": "at least one specific way this week's learning will influence your research approach, your questions, or your design decisions going forward",
            "opening_prompt": (
                "How does what you learned this week connect to your team's capstone project? "
                "Describe at least one specific way this week's learning will influence your "
                "research approach, your questions, or your design decisions going forward."
            ),
            "depth_probe": None,
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
    "version": "2.0.0",
    "title": "HCI 271 — Weekly Reflection Journal (Parts 2 & 3: Team)",
    "course": "HCI 271 — Capstone I",
    "instructor": "Magy Seif El-Nasr",
    "week": 6,
    "intro": (
        "I'll walk you through 6 team-process prompts from Parts 2 and 3 of the Weekly "
        "Reflection Journal template (Team Process & Dynamics, and Looking Forward). "
        "This is the team-process companion to your individual reflection. "
        "You can ask to revise an earlier answer at any point. Type STOP to end early. "
        "When we finish, you'll get a file to upload to the course portal."
    ),
    "parts_blurb": "from Parts 2 and 3 of the Weekly Reflection Journal template",
    "transition_template": "Thanks — anything else on {{section_topic}} before we move on?",
    "advance_template": "Got it. Now let's switch to {{next_section_title}} — {{next_section_one_line}}.",
    "shallow_word_threshold": 25,
    "max_probes_per_section": 1,
    "sections": [
        {
            "id": "2.1",
            "title": "Planning & Execution",
            "topic": "how your team planned its work for the week",
            "one_line": "shared understanding of goals, division and sequencing of work, and whether you followed or adapted the plan",
            "opening_prompt": (
                "How did your team plan its work for the week? Did you have a shared "
                "understanding of the week's goals and tasks before you began, and how "
                "did you divide and sequence the work?"
            ),
            "depth_probe": (
                "Did you follow your plan, or did you adapt? If you adapted, what caused "
                "the change, and was it the right call?"
            ),
            "fields": [{"id": "2.1", "kind": "longform"}],
        },
        {
            "id": "2.2",
            "title": "Roles & Contributions",
            "topic": "your specific contributions and the team's overall division of labor",
            "one_line": "each teammate's primary role / contribution this week, plus an equity reflection",
            "opening_prompt": (
                "Let's capture each teammate's primary role or contribution this week. "
                "Who's on your team — list everyone, including yourself?"
            ),
            "depth_probe": (
                "Was the distribution of work equitable and appropriate given each person's "
                "strengths? If not, what would you change?"
            ),
            "fields": [
                {
                    "id": "2.2.roster",
                    "kind": "table",
                    "columns": ["Team Member", "Primary Role / Contribution This Week"],
                    "min_rows": 2,
                },
                {
                    "id": "2.2.equity",
                    "kind": "shortform",
                    "label": "Was the distribution of work equitable and appropriate given each person's strengths? If not, what would you change?",
                },
            ],
            "collection_strategy": "ask for the full roster up front, then walk member-by-member for contributions, then ask the equity question.",
        },
        {
            "id": "2.3",
            "title": "Collaboration & Communication",
            "topic": "how your team communicated and made decisions this week",
            "one_line": "what worked well, what was challenging, one actionable improvement for next week",
            "opening_prompt": (
                "Reflect on how your team communicated and made decisions this week. What "
                "worked well — describe a specific communication practice, meeting structure, "
                "or collaborative moment that was effective. Be concrete: what exactly "
                "happened and why did it work?"
            ),
            "depth_probe": (
                "Now what was challenging — describe a point of friction, miscommunication, "
                "or inefficiency. Avoid vague statements like \"communication was hard\" — "
                "what specifically broke down, and what was the impact? And then: propose "
                "one concrete, specific, measurable improvement for next week."
            ),
            "fields": [
                {"id": "2.3.worked", "kind": "shortform", "label": "What worked well (concrete)"},
                {"id": "2.3.challenge", "kind": "shortform", "label": "What was challenging (specific)"},
                {"id": "2.3.improvement", "kind": "shortform", "label": "One actionable improvement for next week (measurable / observable)"},
            ],
        },
        {
            "id": "2.4",
            "title": "Team Health Check",
            "topic": "your 1–5 ratings on team functioning this week",
            "one_line": "rate your team on five dimensions (1 = Strongly Disagree, 5 = Strongly Agree) and briefly justify each",
            "opening_prompt": (
                "Now rate your team's functioning this week on five dimensions. Use a 1–5 "
                "scale (1 = Strongly Disagree, 5 = Strongly Agree) and briefly justify each "
                "rating. Ready?"
            ),
            "depth_probe": None,
            "collection_strategy": "ask justification first, then rating, for each dimension — to prevent default-fives.",
            "fields": [
                {"id": "2.4.shared_goal",   "kind": "rating_with_justification", "dimension": "We had a clear, shared goal for the week."},
                {"id": "2.4.heard",         "kind": "rating_with_justification", "dimension": "Everyone's contributions were valued and heard."},
                {"id": "2.4.disagreements", "kind": "rating_with_justification", "dimension": "We resolved disagreements constructively."},
                {"id": "2.4.commitments",   "kind": "rating_with_justification", "dimension": "We met our commitments and deadlines to each other."},
                {"id": "2.4.confidence",    "kind": "rating_with_justification", "dimension": "I feel confident about our direction going into next week."},
            ],
        },
        {
            "id": "3.1",
            "title": "Our Biggest Open Question",
            "topic": "the one question you most need to answer before you can move forward with confidence",
            "one_line": "one question — about your users, your problem space, your methods, or your team — that you most need to answer next",
            "opening_prompt": (
                "What is the one question — about your users, your problem space, your "
                "methods, or your team — that you most need to answer before you can move "
                "forward with confidence?"
            ),
            "depth_probe": None,
            "fields": [{"id": "3.1", "kind": "longform"}],
        },
        {
            "id": "3.2",
            "title": "Our Commitment for Next Week",
            "topic": "one specific commitment your team is making for the coming week",
            "one_line": "one specific commitment — process improvement, research action, or design decision",
            "opening_prompt": (
                "Based on your reflections above, state one specific commitment your team "
                "is making for the coming week. This could be a process improvement, a "
                "research action, or a design decision."
            ),
            "depth_probe": None,
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


# ─── 0024 bodies (used by downgrade only) ────────────────────────────────────
# Verbatim from migration 0024 so that reverting this migration restores the
# previous (non-PDF-aligned) state without losing prior fields.

LEGACY_FORM_BODY_V3 = {
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
        {"id": "1.1", "title": "Key Concepts & Takeaways", "topic": "the concept that stuck with you this week", "one_line": "the single concept or skill from Week 6 that stood out", "opening_prompt": "What's one idea from this week's data analysis session that actually stuck with you, and why?", "depth_probe": "Can you anchor that in something concrete — a moment in lecture, a slide, a sticky note, or a piece of your team's data — that made it click?", "fields": [{"id": "1.1", "kind": "longform", "target_words": "2–3 paragraphs"}]},
        {"id": "1.2", "title": "Methods in Practice", "topic": "the synthesis method you actually applied this week", "one_line": "a specific synthesis method you used (affinity diagramming, thematic coding, journey mapping, etc.)", "opening_prompt": "Which synthesis method did you actually try with your team this week, and how did it feel in practice versus how it was described in lecture?", "depth_probe": "Did you hit any of the NN/g pitfalls — clustering by topic instead of behavior, premature labelling, one person driving the wall, going abstract too fast? Pick one and tell me what happened.", "fields": [{"id": "1.2", "kind": "longform", "target_words": "2–3 paragraphs", "subprompts": ["how you applied it", "practice vs theory", "results and learning"]}]},
        {"id": "1.3", "title": "Knowledge Shift: Before vs. After", "topic": "how your understanding changed this week", "one_line": "what you thought before, what surprised you, what's still uncertain", "opening_prompt": "Before this week, what did you assume about turning raw data into design direction?", "depth_probe": "What specifically challenged that assumption — a piece of evidence, a teammate's point, a moment in the affinity wall?", "fields": [{"id": "1.3a", "kind": "shortform", "label": "What I thought I knew"}, {"id": "1.3b", "kind": "shortform", "label": "What I was surprised by"}, {"id": "1.3c", "kind": "shortform", "label": "What I am still uncertain about"}]},
        {"id": "1.4", "title": "Connection to the Capstone Project", "topic": "how this week's learning lands in your team's project", "one_line": "one specific way this week's learning will change your project going forward", "opening_prompt": "How will what you learned this week change your team's research approach, your interview questions, or an emerging design decision?", "depth_probe": "What's the smallest concrete change you'd make next week as a result?", "fields": [{"id": "1.4", "kind": "longform"}]},
    ],
    "closing": {"behavior": "After all sections have at least one student response and the student signals done, ask one feedback question about the bot itself, then emit [END] on its own line.", "feedback_prompt": "Last thing — did this conversation surface more honest reflection than filling out the PDF would have, and what would make it work better next week?"},
    "ordering_rules": {"strict_in_order": True, "must_cover_all_sections": True, "stop_warn_then_honor": True},
}


LEGACY_TEAM_BODY_V1 = {
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
        {"id": "2.1", "title": "Planning & Execution", "topic": "how your team planned and executed this week", "one_line": "your team's plan, division of work, and adaptation", "opening_prompt": "Did your team have a shared understanding of the week's goals before you started, or did that emerge as you went?", "depth_probe": "If you adapted the plan mid-week, what triggered the change and was it the right call in hindsight?", "fields": [{"id": "2.1", "kind": "longform"}]},
        {"id": "2.2", "title": "Roles & Contributions", "topic": "who did what on your team this week", "one_line": "list each teammate and their primary contribution this week", "opening_prompt": "Let's capture what each teammate worked on this week. Who's on your team — list everyone, including yourself?", "depth_probe": "Was the distribution of work equitable given each person's strengths? If not, what would you change?", "fields": [{"id": "2.2.roster", "kind": "table", "columns": ["Team Member", "Primary Role / Contribution This Week"], "min_rows": 2}, {"id": "2.2.equity", "kind": "shortform", "label": "Was distribution equitable? If not, what would you change?"}], "collection_strategy": "ask for the full roster up front, then walk member-by-member for contributions, then ask the equity question."},
        {"id": "2.3", "title": "Collaboration & Communication", "topic": "how your team communicated and made decisions", "one_line": "what worked, what was challenging, one actionable improvement", "opening_prompt": "Tell me about one specific communication moment from this week that worked well — what exactly happened?", "depth_probe": "Now flip it — what specifically broke down? Avoid 'communication was hard' — give me the concrete moment and its impact.", "fields": [{"id": "2.3.worked", "kind": "shortform", "label": "What worked well (concrete)"}, {"id": "2.3.challenge", "kind": "shortform", "label": "What was challenging (specific)"}, {"id": "2.3.improvement", "kind": "shortform", "label": "One actionable improvement for next week (measurable)"}]},
        {"id": "2.4", "title": "Team Health Check", "topic": "your 1–5 ratings on team functioning", "one_line": "five 1–5 ratings with brief justifications", "opening_prompt": "Now five quick team-health ratings on a 1–5 scale (1 = strongly disagree, 5 = strongly agree), each with a one-sentence why. Ready?", "depth_probe": None, "collection_strategy": "ask justification first, then rating, for each dimension — to prevent default-fives.", "fields": [{"id": "2.4.shared_goal", "kind": "rating_with_justification", "dimension": "We had a clear, shared goal for the week."}, {"id": "2.4.heard", "kind": "rating_with_justification", "dimension": "Everyone's contributions were valued and heard."}, {"id": "2.4.disagreements", "kind": "rating_with_justification", "dimension": "We resolved disagreements constructively."}, {"id": "2.4.commitments", "kind": "rating_with_justification", "dimension": "We met our commitments and deadlines to each other."}, {"id": "2.4.confidence", "kind": "rating_with_justification", "dimension": "I feel confident about our direction going into next week."}]},
        {"id": "3.1", "title": "Our Biggest Open Question", "topic": "the one question your team most needs to answer next", "one_line": "the open question (about users, problem space, methods, or team)", "opening_prompt": "What is the single question — about your users, your problem, your method, or your team — that your team most needs to answer before you can move forward with confidence?", "depth_probe": "Why that one specifically? What makes it the bottleneck right now?", "fields": [{"id": "3.1", "kind": "longform"}]},
        {"id": "3.2", "title": "Our Commitment for Next Week", "topic": "your team's concrete commitment for next week", "one_line": "one specific commitment your team is making", "opening_prompt": "Based on everything you just reflected on, what's one concrete commitment your team is making for next week — process change, research action, or design decision?", "depth_probe": "How will you know you actually did it? What's the observable signal?", "fields": [{"id": "3.2", "kind": "longform"}]},
    ],
    "closing": {"behavior": "After all sections have at least one student response and the student signals done, ask one feedback question about the bot itself, then emit [END] on its own line.", "feedback_prompt": "Last thing — did this team-process conversation surface more honest reflection than filling out the PDF would have, and what would make it work better next week?"},
    "ordering_rules": {"strict_in_order": True, "must_cover_all_sections": True, "stop_warn_then_honor": True},
}


def _upsert_body(FormSchema, body):
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


def upgrade(apps, schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    _upsert_body(FormSchema, HCI271_WK6_FORM_BODY)
    _upsert_body(FormSchema, HCI271_WK6_TEAM_BODY)


def downgrade(apps, schema_editor):
    FormSchema = apps.get_model("datapipeline", "FormSchema")
    _upsert_body(FormSchema, LEGACY_FORM_BODY_V3)
    _upsert_body(FormSchema, LEGACY_TEAM_BODY_V1)


class Migration(migrations.Migration):
    dependencies = [
        ("datapipeline", "0024_split_hci271_part1_and_team_schemas"),
    ]
    operations = [migrations.RunPython(upgrade, downgrade)]
