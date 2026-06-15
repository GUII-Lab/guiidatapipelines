#!/usr/bin/env python
"""Seed CMPM 80H reflection surveys, schemas, and team snapshots into the
local ciba DB so the LEAI prompt simulator can drive conversations against
them. Idempotent — safe to re-run after editing the prompt .md files.

Run from the guiidatapipelines repo root:
    source .venv/bin/activate && python scripts/seed_cmpm80h.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'guiidatapipelines.settings')
import django  # noqa: E402
django.setup()

from datapipeline.models import (  # noqa: E402
    Course, FeedbackGPT, FormSchema, TeamConfiguration, Team,
    SurveyTeamSnapshot, SurveyTeam,
)

PROMPTS = Path(
    '/Users/harveyli/Documents/GitHub/GUII-Lab.github.io/LEAI/docs/prompts'
)
COURSE_LABEL = 'CMPM 80H — Human Centered AI'
INSTRUCTOR = 'Magy Seif El-Nasr'

# ── schema bodies ──────────────────────────────────────────────────────────

REFLECTION_BODY = {
    'schema_id': 'cmpm80h-reflection',
    'version': '1.0.0',
    'week': None,
    'title': 'CMPM 80H — Weekly Reflection Journal (Part 1: Individual)',
    'course': COURSE_LABEL,
    'instructor': INSTRUCTOR,
    'parts_blurb': 'from Part 1 of the Weekly Reflection Journal template',
    'intro': "I'll walk you through 3 personal-reflection prompts from Part 1 "
             "of the Weekly Reflection Journal template (Individual Learning & "
             "Discovery). Your team-process reflection (Parts 2 and 3) is a "
             "separate survey. You can ask to revise an earlier answer at any "
             "point. When we finish, you'll get a file to upload to the course "
             "portal.",
    'closing': {
        'behavior': 'After all sections have at least one student response and '
                    'the student signals done, ask one feedback question about '
                    'the bot itself, then emit [END] on its own line.',
        'feedback_prompt': 'Last thing — did this conversation surface more '
                           'honest reflection than filling out the PDF would '
                           'have, and what would make it work better next time?',
    },
    'ordering_rules': {
        'strict_in_order': True,
        'stop_warn_then_honor': True,
        'must_cover_all_sections': True,
    },
    'advance_template': "Got it. Now let's switch to {{next_section_title}} — "
                        "{{next_section_one_line}}.",
    'transition_template': 'Thanks — anything else on {{section_topic}} before '
                           'we move on?',
    'max_probes_per_section': 1,
    'shallow_word_threshold': 25,
    'sections': [
        {
            'id': '1.1',
            'title': 'Key Concepts & Takeaways',
            'topic': 'the single most important concept or idea from this week',
            'one_line': 'the single most important concept or idea introduced '
                        'this week, in your own words',
            'opening_prompt': 'What was the single most important concept or '
                              'idea introduced this week? Describe it in your '
                              'own words — avoid simply restating the lecture '
                              'definition.',
            'depth_probe': 'Why did it stand out to you, and how do you '
                           'currently understand it?',
            'fields': [{'id': '1.1', 'kind': 'longform',
                        'target_words': '2–3 paragraphs'}],
        },
        {
            'id': '1.2',
            'title': 'Methods in Practice',
            'topic': 'the main activity, method, or experiment you did this week',
            'one_line': 'the main activity, method, or experiment you did this '
                        'week and what doing it taught you',
            'opening_prompt': 'Describe the main activity, method, or experiment '
                              'you did this week. How did you go about it?',
            'depth_probe': 'What did doing it feel like in practice versus how '
                           'it was described?',
            'fields': [{'id': '1.2', 'kind': 'longform',
                        'target_words': '2–3 paragraphs',
                        'subprompts': ['what you actually did',
                                       'practice vs. theory',
                                       'results and what you learned from doing it']}],
        },
        {
            'id': '1.3',
            'title': 'Knowledge Shift: Before vs. After',
            'topic': 'the gap between your prior understanding and your current one',
            'one_line': 'what you thought you knew, what surprised you, what you '
                        'are still uncertain about',
            'opening_prompt': 'Before this week, what did you think you knew '
                              'or assume about this topic?',
            'depth_probe': 'Now — what specifically surprised you this week, '
                           'and what shifted because of it?',
            'fields': [
                {'id': '1.3a', 'kind': 'shortform', 'label': 'What I thought I knew'},
                {'id': '1.3b', 'kind': 'shortform', 'label': 'What I was surprised by'},
                {'id': '1.3c', 'kind': 'shortform', 'label': 'What I am still uncertain about'},
            ],
        },
    ],
}

TEAM_BODY = {
    'schema_id': 'cmpm80h-team-reflection',
    'version': '1.0.0',
    'week': None,
    'title': 'CMPM 80H — Weekly Reflection Journal (Parts 2 & 3: Team)',
    'course': COURSE_LABEL,
    'instructor': INSTRUCTOR,
    'parts_blurb': 'from Parts 2 and 3 of the Weekly Reflection Journal template',
    'intro': "I'll walk you through 6 team-process prompts from Parts 2 and 3 of "
             "the Weekly Reflection Journal template (Team Process & Dynamics, "
             "and Looking Forward). This is the team-process companion to your "
             "individual reflection. You can ask to revise an earlier answer at "
             "any point. When we finish, you'll get a file to upload to the "
             "course portal.",
    'closing': {
        'behavior': 'After all sections have at least one student response and '
                    'the student signals done, ask one feedback question about '
                    'the bot itself, then emit [END] on its own line.',
        'feedback_prompt': 'Last thing — was this team-process conversation '
                           'useful, and what would make it work better next time?',
    },
    'ordering_rules': {
        'strict_in_order': True,
        'stop_warn_then_honor': True,
        'must_cover_all_sections': True,
    },
    'advance_template': "Got it. Now let's switch to {{next_section_title}} — "
                        "{{next_section_one_line}}.",
    'transition_template': 'Thanks — anything else on {{section_topic}} before '
                           'we move on?',
    'max_probes_per_section': 1,
    'shallow_word_threshold': 25,
    'sections': [
        {
            'id': '2.1',
            'title': 'Planning & Execution',
            'topic': 'how your team planned and sequenced its work this week',
            'one_line': 'shared understanding of goals, division and sequencing '
                        'of work, and whether you followed or adapted the plan',
            'opening_prompt': 'How did your team plan and divide the work this '
                              'week, and did you share a clear understanding of '
                              'the goals before you began?',
            'depth_probe': 'Did you end up adapting the plan as the week went, '
                           'and what triggered it?',
            'fields': [{'id': '2.1', 'kind': 'longform'}],
        },
        {
            'id': '2.2',
            'title': 'Roles & Contributions',
            'topic': "your specific contributions and the team's division of labor",
            'one_line': "each teammate's primary role / contribution this week, "
                        'plus an equity reflection',
            'opening_prompt': "Let's capture each teammate's primary role or "
                              'contribution this week. Who is on your team — list '
                              'everyone, including yourself?',
            'depth_probe': 'Was the distribution of work equitable this week, '
                           "given each person's strengths?",
            'collection_strategy': 'ask for the full roster up front, then walk '
                                   'member-by-member for contributions, then ask '
                                   'the equity question.',
            'fields': [
                {'id': '2.2.roster', 'kind': 'table',
                 'columns': ['Team Member', 'Primary Role / Contribution This Week'],
                 'min_rows': 2},
                {'id': '2.2.equity', 'kind': 'shortform',
                 'label': 'Was the distribution of work equitable and appropriate '
                          "given each person's strengths? If not, what would you change?"},
            ],
        },
        {
            'id': '2.3',
            'title': 'Collaboration & Communication',
            'topic': 'what worked well, what was challenging, one improvement',
            'one_line': 'what worked well, what was challenging, one actionable '
                        'improvement for next week',
            'opening_prompt': "What's one specific collaboration moment that "
                              'worked well for your team this week?',
            'depth_probe': 'Now what was challenging — describe a point of '
                           'friction, miscommunication, or inefficiency. Avoid '
                           'vague statements like "communication was hard" — what '
                           'specifically broke down, and what was the impact? And '
                           'then: propose one concrete, measurable improvement '
                           'for next week.',
            'fields': [
                {'id': '2.3.worked', 'kind': 'shortform', 'label': 'What worked well (concrete)'},
                {'id': '2.3.challenge', 'kind': 'shortform', 'label': 'What was challenging (specific)'},
                {'id': '2.3.improvement', 'kind': 'shortform', 'label': 'One actionable improvement for next week (measurable / observable)'},
            ],
        },
        {
            'id': '2.4',
            'title': 'Team Health Check',
            'topic': 'your 1–5 ratings on team functioning this week',
            'one_line': 'rate your team on five dimensions (1 = Strongly '
                        'Disagree, 5 = Strongly Agree) and briefly justify each',
            'opening_prompt': "Now rate your team's functioning this week on five "
                              'dimensions. Use a 1–5 scale (1 = Strongly '
                              'Disagree, 5 = Strongly Agree) and briefly justify '
                              'each rating. Ready?',
            'depth_probe': None,
            'collection_strategy': 'ask justification first, then rating, for '
                                   'each dimension — to prevent default-fives.',
            'fields': [
                {'id': '2.4.shared_goal', 'kind': 'rating_with_justification', 'dimension': 'We had a clear, shared goal for the week.'},
                {'id': '2.4.heard', 'kind': 'rating_with_justification', 'dimension': "Everyone's contributions were valued and heard."},
                {'id': '2.4.disagreements', 'kind': 'rating_with_justification', 'dimension': 'We resolved disagreements constructively.'},
                {'id': '2.4.commitments', 'kind': 'rating_with_justification', 'dimension': 'We met our commitments and deadlines to each other.'},
                {'id': '2.4.confidence', 'kind': 'rating_with_justification', 'dimension': 'I feel confident about our direction going into next week.'},
            ],
        },
        {
            'id': '3.1',
            'title': 'Our Biggest Open Question',
            'topic': 'the one question you most need to answer next',
            'one_line': 'one question — about your users, your problem space, '
                        'your methods, or your team — that you most need to '
                        'answer next',
            'opening_prompt': 'What is the one question your team most needs to '
                              'answer before you can move forward with '
                              'confidence?',
            'depth_probe': None,
            'fields': [{'id': '3.1', 'kind': 'longform'}],
        },
        {
            'id': '3.2',
            'title': 'Our Commitment for Next Week',
            'topic': 'one specific commitment your team is making',
            'one_line': 'one specific commitment — process improvement, research '
                        'action, or design decision',
            'opening_prompt': 'Based on your reflections above, state one '
                              'specific commitment your team is making for the '
                              'coming week.',
            'depth_probe': None,
            'fields': [{'id': '3.2', 'kind': 'longform'}],
        },
    ],
}

# ── survey definitions ─────────────────────────────────────────────────────
# (week, mode, public_id, name, prompt_file, schema_id)
SURVEYS = [
    (1, 'form',  'c80h-w1-form', 'CMPM 80H Wk1 Form (Individual)',  'wk1-cmpm80h-form.md',  'cmpm80h-reflection'),
    (2, 'form',  'c80h-w2-form', 'CMPM 80H Wk2 Form (Individual)',  'wk2-cmpm80h-form.md',  'cmpm80h-reflection'),
    (2, 'group', 'c80h-w2-grp',  'CMPM 80H Wk2 Group (Team)',       'wk2-cmpm80h-group.md', 'cmpm80h-team-reflection'),
    (3, 'form',  'c80h-w3-form', 'CMPM 80H Wk3 Form (Individual)',  'wk3-cmpm80h-form.md',  'cmpm80h-reflection'),
    (3, 'group', 'c80h-w3-grp',  'CMPM 80H Wk3 Group (Team)',       'wk3-cmpm80h-group.md', 'cmpm80h-team-reflection'),
    (4, 'form',  'c80h-w4-form', 'CMPM 80H Wk4 Form (Individual)',  'wk4-cmpm80h-form.md',  'cmpm80h-reflection'),
    (4, 'group', 'c80h-w4-grp',  'CMPM 80H Wk4 Group (Team)',       'wk4-cmpm80h-group.md', 'cmpm80h-team-reflection'),
    (5, 'form',  'c80h-w5-form', 'CMPM 80H Wk5 Form (Individual)',  'wk5-cmpm80h-form.md',  'cmpm80h-reflection'),
    (5, 'group', 'c80h-w5-grp',  'CMPM 80H Wk5 Group (Team)',       'wk5-cmpm80h-group.md', 'cmpm80h-team-reflection'),
]


def main():
    # 1) schemas
    for body in (REFLECTION_BODY, TEAM_BODY):
        obj, created = FormSchema.objects.update_or_create(
            schema_id=body['schema_id'],
            defaults=dict(
                version=body['version'], title=body['title'],
                course_label=COURSE_LABEL, week_number=None,
                body=body, is_active=True,
            ),
        )
        print(f"  schema {obj.schema_id:28s} {'created' if created else 'updated'}")

    schemas = {s.schema_id: s for s in FormSchema.objects.filter(
        schema_id__in=['cmpm80h-reflection', 'cmpm80h-team-reflection'])}

    # 2) course + team configuration (for the group surveys)
    course, _ = Course.objects.get_or_create(
        course_id='cmpm80h-sp26',
        defaults=dict(course_name=COURSE_LABEL, instructor_name=INSTRUCTOR),
    )
    tc, _ = TeamConfiguration.objects.get_or_create(
        course=course, name='80H Project Teams',
        defaults=dict(label_prefix='Team', color='forest'),
    )
    for num in (1, 2):
        Team.objects.get_or_create(
            team_configuration=tc, number=num, defaults=dict(size=4))
    print(f"  course {course.course_id} + team config '{tc.name}' ready")

    # 3) surveys
    missing = []
    for week, mode, pub, name, fname, schema_id in SURVEYS:
        path = PROMPTS / fname
        if not path.exists():
            missing.append(str(path))
            continue
        instructions = path.read_text()
        gpt, created = FeedbackGPT.objects.update_or_create(
            public_id=pub,
            defaults=dict(
                name=name, instructions=instructions, week_number=week,
                mode=mode, form_schema=schemas[schema_id], course=course,
                created_by='seed_cmpm80h', is_closed=False,
                anonymity_mode='identified' if mode == 'group' else 'anonymous',
                survey_label=f'CMPM 80H — Week {week}',
            ),
        )
        # group surveys need a team snapshot
        if mode == 'group':
            snap, _ = SurveyTeamSnapshot.objects.update_or_create(
                survey=gpt,
                defaults=dict(source_configuration=tc, name=tc.name,
                              label_prefix=tc.label_prefix, color=tc.color),
            )
            for num in (1, 2):
                SurveyTeam.objects.update_or_create(
                    snapshot=snap, number=num, defaults=dict(size=4))
        print(f"  survey {pub:14s} wk{week} {mode:5s} {'created' if created else 'updated'}  "
              f"({len(instructions)} chars)")

    if missing:
        print('\n  !! MISSING PROMPT FILES:')
        for m in missing:
            print('    ', m)
        sys.exit(1)

    print('\n  done. public_ids:', ', '.join(s[2] for s in SURVEYS))


if __name__ == '__main__':
    main()
