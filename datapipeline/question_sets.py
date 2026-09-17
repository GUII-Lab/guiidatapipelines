from __future__ import annotations

import copy
import hashlib
import json
import math
import secrets
import string
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.db.models import Q
from django.utils import timezone

from .instructor_audit import record_instructor_event
from .instructor_auth import token_digest
from .models import (
    Course,
    FeedbackGPT,
    InstructorAuditEvent,
    PreviewMessage,
    PreviewSession,
    QuestionSet,
    QuestionSetDraft,
    QuestionSetDraftVersion,
    QuestionSetMutationReceipt,
    QuestionSetRevision,
    QuestionSetSurvey,
    QuestionSetTemplate,
    QuestionSetTemplateRevision,
    QuestionSetValidationRun,
    SurveyTeam,
    SurveyTeamSnapshot,
    TeamConfiguration,
)


COMPILER_VERSION = '1'
ENGINE_VERSION = 'formmode-v1'
PREVIEW_TTL = timedelta(hours=12)
PREVIEW_READY_DELAY_MIN_MS = 2600
PREVIEW_READY_DELAY_MAX_MS = 3800
MAX_TEXT_LENGTH = 4000
MAX_GUIDED_SECTIONS = 12
MAX_GUIDED_QUESTIONS = 24
MAX_CANONICAL_BODY_BYTES = 64 * 1024
CHECKPOINT_INTERVAL = timedelta(seconds=30)
FORCED_CHECKPOINT_REASONS = {'ai', 'preview', 'publish', 'leave', 'restore'}

# Question-set surveys deliberately start with the smallest anonymous student
# surface. These values are copied into every immutable compiled revision so a
# later course-level customization cannot silently change a published survey.
QUESTION_SET_EFFECTIVE_SETTINGS = {
    'course_banner': None,
    'bot_display_name': 'LEAI',
    'referral_enabled': False,
    'referral_text': '',
    'identity_tracking_enabled': False,
    'completion_certificate_enabled': False,
    'parsed_document_download_enabled': False,
}


def _section(section_id, title, prompt, probe):
    return {
        'id': section_id,
        'title': title,
        'topic': title.lower(),
        'one_line': title.lower(),
        'opening_prompt': prompt,
        'depth_probe': probe,
        'fields': [
            {
                'id': section_id,
                'kind': 'longform',
                'label': prompt,
            },
        ],
    }


def _body(title, intro, sections, closing_prompt):
    return {
        'title': title,
        'intro': intro,
        'transition_template': (
            'Thanks — anything else on {{section_topic}} before we move on?'
        ),
        'advance_template': (
            'Got it. Now let’s switch to {{next_section_title}} — '
            '{{next_section_one_line}}.'
        ),
        'shallow_word_threshold': 25,
        'max_probes_per_section': 1,
        'sections': sections,
        'closing': {
            'behavior': (
                'After all sections have at least one student response and the '
                'student signals done, ask the feedback question, then emit [END] '
                'on its own line.'
            ),
            'feedback_prompt': closing_prompt,
        },
        'ordering_rules': {
            'strict_in_order': True,
            'must_cover_all_sections': True,
            'stop_warn_then_honor': True,
        },
    }


SYSTEM_TEMPLATES = (
    {
        'id': 'weekly-reflection',
        'name': 'Weekly reflection',
        'description': 'A balanced weekly check-in on learning, difficulty, and next steps.',
        'audience': QuestionSet.AUDIENCE_INDIVIDUAL,
        'body': _body(
            'Weekly learning reflection',
            'A short guided conversation about this week’s learning experience.',
            [
                _section(
                    'q1',
                    'What stood out',
                    'What idea, activity, or moment stood out most to you this week, and why?',
                    'Can you point to a specific moment that made it stand out?',
                ),
                _section(
                    'q2',
                    'What was difficult',
                    'What felt confusing, difficult, or slower than you expected this week?',
                    'What would have helped you make progress sooner?',
                ),
                _section(
                    'q3',
                    'Learning support',
                    'What did the instructor, course materials, or your classmates do that helped your learning?',
                    'Which part of that support should continue next week?',
                ),
                _section(
                    'q4',
                    'Next step',
                    'What is one concrete step you will take before the next class?',
                    'How will you know you completed that step?',
                ),
            ],
            'Before you finish, what is one change that could improve next week’s learning experience?',
        ),
    },
    {
        'id': 'mid-course-check-in',
        'name': 'Mid-course check-in',
        'description': 'A broader check on course pace, support, confidence, and priorities.',
        'audience': QuestionSet.AUDIENCE_INDIVIDUAL,
        'body': _body(
            'Mid-course learning check-in',
            'A guided conversation to understand how the course is working so far.',
            [
                _section(
                    'q1',
                    'Current confidence',
                    'How confident do you feel about the main ideas and skills covered so far?',
                    'What evidence from your recent work shaped that answer?',
                ),
                _section(
                    'q2',
                    'Course pace',
                    'How is the pace of the course working for you right now?',
                    'Where would slowing down or moving faster help most?',
                ),
                _section(
                    'q3',
                    'Helpful support',
                    'Which course activity or resource has helped your learning most so far?',
                    'What about it made it useful?',
                ),
                _section(
                    'q4',
                    'Priority for the rest of the course',
                    'What should the instructor prioritize during the rest of the course?',
                    'What difference would that change make for your learning?',
                ),
            ],
            'Is there anything else the instructor should understand about your experience so far?',
        ),
    },
    {
        'id': 'project-milestone',
        'name': 'Project milestone reflection',
        'description': 'An individual reflection on progress, decisions, obstacles, and the next milestone.',
        'audience': QuestionSet.AUDIENCE_INDIVIDUAL,
        'body': _body(
            'Project milestone reflection',
            'A guided individual reflection on your project work and next decisions.',
            [
                _section(
                    'q1',
                    'Progress',
                    'What meaningful progress did you make toward this milestone?',
                    'Which specific artifact, decision, or result best shows that progress?',
                ),
                _section(
                    'q2',
                    'Important decision',
                    'What was the most important decision you made, and what informed it?',
                    'What alternative did you consider?',
                ),
                _section(
                    'q3',
                    'Obstacle',
                    'What obstacle or uncertainty affected your work most?',
                    'What support or information would help you address it?',
                ),
                _section(
                    'q4',
                    'Next milestone',
                    'What is your most important next action before the next milestone?',
                    'What observable result will show that action is complete?',
                ),
            ],
            'What is one thing your instructor should know when reviewing your progress?',
        ),
    },
)


class QuestionSetError(Exception):
    def __init__(self, code, message=None, timing=None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.timing = timing


def list_templates(*, actor=None, audience=None, collection_style=None):
    """Return the legacy catalog or an authorized v12 revision catalog.

    Calling without filters preserves the first-release Wizard response while
    the v12 frontend is introduced. The filtered form is revision-pinned and
    never exposes another instructor's private template.
    """
    if audience is None and collection_style is None:
        return copy.deepcopy(list(SYSTEM_TEMPLATES))
    _validate_feedback_taxonomy(audience, collection_style)
    if actor is None:
        raise QuestionSetError('authentication_required')
    templates = (
        QuestionSetTemplate.objects
        .filter(
            audience=audience,
            collection_style=collection_style,
            is_active=True,
        )
        .filter(
            Q(scope=QuestionSetTemplate.SCOPE_GLOBAL)
            | Q(owner=actor)
            | Q(
                visibility=QuestionSetTemplate.VISIBILITY_COMMUNITY,
                community_revision__isnull=False,
            )
        )
        .select_related('owner', 'community_revision')
        .prefetch_related('revisions')
        .order_by('name', 'id')
    )
    result = []
    for template in templates:
        if template.owner_id == actor.pk:
            revision = template.revisions.order_by('-revision_number', '-id').first()
            source = 'mine'
        else:
            revision = template.community_revision
            source = 'leai' if template.scope == QuestionSetTemplate.SCOPE_GLOBAL else 'community'
        if revision is None:
            continue
        result.append({
            'id': str(template.public_id),
            'revision_id': str(revision.public_id),
            'name': template.name,
            'description': template.description,
            'audience': template.audience,
            'collection_style': template.collection_style,
            'source': source,
            'owner_display_name': template.owner.display_name if template.owner_id else None,
            'revision_number': revision.revision_number,
        })
    return result


def get_template(template_id):
    for template in SYSTEM_TEMPLATES:
        if template['id'] == template_id:
            return copy.deepcopy(template)
    raise QuestionSetError('template_not_found')


def _bounded_text(value, field_name, *, required=True, max_length=MAX_TEXT_LENGTH):
    if not isinstance(value, str):
        raise QuestionSetError('invalid_question_set', f'{field_name} must be text.')
    value = value.strip()
    if required and not value:
        raise QuestionSetError('invalid_question_set', f'{field_name} is required.')
    if len(value) > max_length:
        raise QuestionSetError('invalid_question_set', f'{field_name} is too long.')
    return value


def _validate_feedback_taxonomy(audience, collection_style):
    valid = {
        (QuestionSet.AUDIENCE_INDIVIDUAL, QuestionSet.COLLECTION_GUIDED),
        (QuestionSet.AUDIENCE_INDIVIDUAL, QuestionSet.COLLECTION_OPEN),
        (QuestionSet.AUDIENCE_TEAM, QuestionSet.COLLECTION_GUIDED),
    }
    if (audience, collection_style) not in valid:
        raise QuestionSetError(
            'invalid_feedback_type',
            'Choose Individual Guided, Individual Open, or Team Guided feedback.',
        )


def _stable_uuid(value, field_name):
    if value in (None, ''):
        return str(uuid.uuid4())
    if not isinstance(value, str):
        raise QuestionSetError('invalid_question_set', f'{field_name} must be a UUID.')
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise QuestionSetError('invalid_question_set', f'{field_name} must be a UUID.')


def _validate_body_size(body):
    size = len(_canonical_json(body).encode('utf-8'))
    if size > MAX_CANONICAL_BODY_BYTES:
        raise QuestionSetError(
            'invalid_question_set',
            'Feedback design must be 64 KiB or smaller.',
        )


def _validate_guided_body(body):
    if not isinstance(body, dict):
        raise QuestionSetError('invalid_question_set', 'Feedback design must be an object.')
    if body.get('schema_version') != 'guided-feedback-v2':
        raise QuestionSetError('invalid_question_set', 'Guided feedback schema is invalid.')
    sections = body.get('sections')
    if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_GUIDED_SECTIONS:
        raise QuestionSetError(
            'invalid_question_set',
            f'Use between 1 and {MAX_GUIDED_SECTIONS} sections.',
        )
    result = {
        'schema_version': 'guided-feedback-v2',
        'title': _bounded_text(body.get('title'), 'Title', max_length=200),
        'intro': _bounded_text(body.get('intro'), 'Introduction'),
        'sections': [],
    }
    seen_ids = set()
    question_count = 0
    for section_index, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            raise QuestionSetError('invalid_question_set', 'Each section must be an object.')
        section_id = _stable_uuid(section.get('id'), f'Section {section_index} ID')
        if section_id in seen_ids:
            raise QuestionSetError('invalid_question_set', 'Section and question IDs must be unique.')
        seen_ids.add(section_id)
        questions = section.get('questions')
        if not isinstance(questions, list) or not questions:
            raise QuestionSetError('invalid_question_set', 'Each section needs a question.')
        normalized_questions = []
        for question_index, question in enumerate(questions, start=1):
            if not isinstance(question, dict):
                raise QuestionSetError('invalid_question_set', 'Each question must be an object.')
            question_count += 1
            if question_count > MAX_GUIDED_QUESTIONS:
                raise QuestionSetError(
                    'invalid_question_set',
                    f'Use at most {MAX_GUIDED_QUESTIONS} questions.',
                )
            question_id = _stable_uuid(
                question.get('id'),
                f'Question {question_count} ID',
            )
            if question_id in seen_ids:
                raise QuestionSetError('invalid_question_set', 'Section and question IDs must be unique.')
            seen_ids.add(question_id)
            if question.get('response_kind') != 'long_text':
                raise QuestionSetError(
                    'invalid_question_set',
                    'V12 questions use long-text responses only.',
                )
            follow_up = question.get('follow_up')
            if not isinstance(follow_up, dict) or type(follow_up.get('enabled')) is not bool:
                raise QuestionSetError('invalid_question_set', 'Follow-up settings are invalid.')
            follow_up_prompt = _bounded_text(
                follow_up.get('prompt', ''),
                f'Question {question_count} follow-up',
                required=follow_up['enabled'],
            )
            normalized_questions.append({
                'id': question_id,
                'short_label': _bounded_text(
                    question.get('short_label'),
                    f'Question {question_count} label',
                    max_length=200,
                ),
                'prompt': _bounded_text(
                    question.get('prompt'),
                    f'Question {question_count}',
                ),
                'follow_up': {
                    'enabled': follow_up['enabled'],
                    'prompt': follow_up_prompt,
                },
                'response_kind': 'long_text',
            })
        result['sections'].append({
            'id': section_id,
            'title': _bounded_text(
                section.get('title'),
                f'Section {section_index} title',
                max_length=200,
            ),
            'questions': normalized_questions,
        })
    closing = body.get('closing')
    if not isinstance(closing, dict):
        raise QuestionSetError('invalid_question_set', 'Closing question is required.')
    result['closing'] = {
        'prompt': _bounded_text(closing.get('prompt'), 'Closing question'),
    }
    _validate_body_size(result)
    return result, {'errors': [], 'question_count': question_count}


def _validate_open_body(body):
    if not isinstance(body, dict):
        raise QuestionSetError('invalid_question_set', 'Feedback design must be an object.')
    if body.get('schema_version') != 'open-conversation-v1':
        raise QuestionSetError('invalid_question_set', 'Open conversation schema is invalid.')
    result = {
        'schema_version': 'open-conversation-v1',
        'title': _bounded_text(body.get('title'), 'Title', max_length=200),
        'opening_prompt': _bounded_text(body.get('opening_prompt'), 'Opening question'),
        'listening_goal': _bounded_text(body.get('listening_goal'), 'Listening goal'),
        'closing_prompt': _bounded_text(body.get('closing_prompt'), 'Closing question'),
    }
    _validate_body_size(result)
    return result, {'errors': [], 'question_count': 1}


def validate_feedback_body(body, *, audience, collection_style):
    _validate_feedback_taxonomy(audience, collection_style)
    if collection_style == QuestionSet.COLLECTION_OPEN:
        return _validate_open_body(body)
    return _validate_guided_body(body)


def _blank_feedback_body(*, audience, collection_style):
    if collection_style == QuestionSet.COLLECTION_OPEN:
        return {
            'schema_version': 'open-conversation-v1',
            'title': 'Course experience check-in',
            'opening_prompt': 'How is your learning experience going right now?',
            'listening_goal': 'Learning supports, blockers, expectations, workload, and suggestions.',
            'closing_prompt': 'Is there anything else you want your instructor to know?',
        }
    team = audience == QuestionSet.AUDIENCE_TEAM
    return {
        'schema_version': 'guided-feedback-v2',
        'title': 'Team collaboration feedback' if team else 'Untitled feedback',
        'intro': (
            'Private feedback about collaboration inside the team you select.'
            if team else
            'A guided conversation about your learning experience.'
        ),
        'sections': [{
            'title': 'Team collaboration' if team else 'New focus area',
            'questions': [{
                'short_label': 'Collaboration experience' if team else 'First question',
                'prompt': (
                    'What is helping or getting in the way of collaboration inside your team?'
                    if team else
                    'What is one important experience you want to reflect on?'
                ),
                'follow_up': {
                    'enabled': True,
                    'prompt': 'Can you describe one concrete example?',
                },
                'response_kind': 'long_text',
            }],
        }],
        'closing': {
            'prompt': 'Is there anything else your instructor should understand?',
        },
    }


def _structure_signature(body):
    try:
        return [
            (
                section['id'],
                tuple((field['id'], field['kind']) for field in section['fields']),
            )
            for section in body['sections']
        ]
    except (KeyError, TypeError):
        raise QuestionSetError('invalid_question_set', 'Question structure is invalid.')


def editable_body(existing, proposed):
    if not isinstance(proposed, dict):
        raise QuestionSetError('invalid_question_set', 'Question set body must be an object.')
    if _structure_signature(existing) != _structure_signature(proposed):
        raise QuestionSetError(
            'invalid_question_set',
            'Question identity and response fields cannot be changed in this release.',
        )
    sections = proposed.get('sections')
    if not isinstance(sections, list) or not 2 <= len(sections) <= 8:
        raise QuestionSetError('invalid_question_set', 'Use between 2 and 8 questions.')

    result = copy.deepcopy(existing)
    result['title'] = _bounded_text(
        proposed.get('title'), 'Title', max_length=200,
    )
    result['intro'] = _bounded_text(proposed.get('intro'), 'Introduction')
    for index, section in enumerate(sections):
        result_section = result['sections'][index]
        result_section['title'] = _bounded_text(
            section.get('title'), f'Question {index + 1} title', max_length=200,
        )
        result_section['topic'] = result_section['title'].lower()
        result_section['one_line'] = result_section['title'].lower()
        result_section['opening_prompt'] = _bounded_text(
            section.get('opening_prompt'), f'Question {index + 1}',
        )
        result_section['depth_probe'] = _bounded_text(
            section.get('depth_probe', ''),
            f'Question {index + 1} follow-up',
            required=False,
        ) or None
        result_section['fields'][0]['label'] = result_section['opening_prompt']
    proposed_closing = proposed.get('closing')
    if not isinstance(proposed_closing, dict):
        raise QuestionSetError('invalid_question_set', 'Closing question is required.')
    result['closing']['feedback_prompt'] = _bounded_text(
        proposed_closing.get('feedback_prompt'), 'Closing question',
    )
    return result


def validate_body(body):
    normalized = editable_body(body, body)
    return normalized, {
        'errors': [],
        'question_count': len(normalized['sections']),
    }


def serialize_draft(draft):
    question_set = draft.question_set
    return {
        'id': str(draft.public_id),
        'question_set_id': str(question_set.public_id),
        'course_id': question_set.course.course_id,
        'template_id': question_set.template_id,
        'title': question_set.title,
        'audience': question_set.audience,
        'collection_style': question_set.collection_style,
        'source_kind': question_set.source_kind,
        'source_template_revision_id': (
            str(question_set.source_template_revision.public_id)
            if question_set.source_template_revision_id else None
        ),
        'workflow_status': question_set.workflow_status,
        'body': copy.deepcopy(draft.body),
        'version': draft.version,
        'base_revision_id': (
            str(draft.base_revision.public_id) if draft.base_revision_id else None
        ),
        'current_checkpoint_id': (
            str(draft.current_checkpoint.public_id)
            if draft.current_checkpoint_id else None
        ),
        'updated_at': draft.updated_at.isoformat(),
    }


def serialize_revision(revision):
    preview_completed = revision.preview_sessions.filter(
        completed_at__isnull=False,
    ).exists()
    return {
        'id': str(revision.public_id),
        'question_set_id': str(revision.question_set.public_id),
        'revision_number': revision.revision_number,
        'source_draft_version': revision.source_draft_version,
        'content_hash': revision.content_hash,
        'compiler_version': revision.compiler_version,
        'engine_version': revision.engine_version,
        'preview_completed': preview_completed,
        'created_at': revision.created_at.isoformat(),
    }


def _record_question_set_event(
    *,
    action,
    actor,
    instructor_session,
    question_set,
    event_id=None,
):
    record_instructor_event(
        event_id=event_id,
        action=action,
        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        actor=actor,
        session=instructor_session,
        course=question_set.course,
        target_type='question_set',
        target_id=question_set.public_id,
        metadata={},
    )


def _validate_optional_uuid(value, *, field_name, conflict_model=None):
    if value is None:
        return
    if type(value) is not uuid.UUID:
        raise QuestionSetError('invalid_deterministic_id', f'{field_name} must be a UUID.')
    if conflict_model is not None and conflict_model.objects.filter(
        public_id=value,
    ).exists():
        raise QuestionSetError(f'{field_name}_conflict')


def _active_question_sets_for_course(course):
    return (
        QuestionSet.objects
        .select_for_update()
        .filter(
            course=course,
            archived_at__isnull=True,
            workflow_status=QuestionSet.WORKFLOW_ACTIVE,
            draft__isnull=False,
        )
        .order_by('-draft__updated_at', '-updated_at', '-id')
    )


def _require_active_workflow(question_set, *, allow_inactive_history=False):
    if (
        not allow_inactive_history
        and question_set.workflow_status != QuestionSet.WORKFLOW_ACTIVE
    ):
        raise QuestionSetError('workflow_not_active')


@transaction.atomic
def abandon_active_draft(*, course, actor, instructor_session):
    abandoned = []
    for question_set in _active_question_sets_for_course(course):
        question_set.workflow_status = QuestionSet.WORKFLOW_ABANDONED
        question_set.save(update_fields=['workflow_status', 'updated_at'])
        _record_question_set_event(
            action=InstructorAuditEvent.ACTION_QUESTION_SET_WORKFLOW_ABANDONED,
            actor=actor,
            instructor_session=instructor_session,
            question_set=question_set,
        )
        abandoned.append(question_set)
    return abandoned


def _authorized_template_revision(*, actor, revision_id, audience, collection_style):
    try:
        revision_uuid = uuid.UUID(str(revision_id))
    except (TypeError, ValueError, AttributeError):
        raise QuestionSetError('template_not_found')
    revision = (
        QuestionSetTemplateRevision.objects
        .select_related('template__owner')
        .filter(public_id=revision_uuid)
        .first()
    )
    if revision is None:
        raise QuestionSetError('template_not_found')
    template = revision.template
    authorized = (
        template.is_active
        and template.audience == audience
        and template.collection_style == collection_style
        and (
            template.owner_id == actor.pk
            or (
                template.visibility == QuestionSetTemplate.VISIBILITY_COMMUNITY
                and template.community_revision_id == revision.pk
            )
        )
    )
    if not authorized:
        raise QuestionSetError('template_not_found')
    return revision


@transaction.atomic
def create_feedback_draft(
    *,
    course,
    actor,
    instructor_session,
    audience,
    collection_style,
    source_kind,
    source_template_revision_id=None,
    question_set_public_id=None,
    draft_public_id=None,
    audit_event_id=None,
    confirm_abandon_active=False,
):
    """Create one v12 draft while atomically replacing a confirmed old draft."""
    _validate_feedback_taxonomy(audience, collection_style)
    if type(confirm_abandon_active) is not bool:
        raise QuestionSetError('invalid_abandon_confirmation')
    if source_kind not in {QuestionSet.SOURCE_TEMPLATE, QuestionSet.SOURCE_BLANK}:
        raise QuestionSetError('invalid_source_kind')
    course = Course.objects.select_for_update().get(pk=course.pk)
    _validate_optional_uuid(
        question_set_public_id,
        field_name='question_set_public_id',
        conflict_model=QuestionSet,
    )
    _validate_optional_uuid(
        draft_public_id,
        field_name='draft_public_id',
        conflict_model=QuestionSetDraft,
    )
    source_revision = None
    if source_kind == QuestionSet.SOURCE_TEMPLATE:
        source_revision = _authorized_template_revision(
            actor=actor,
            revision_id=source_template_revision_id,
            audience=audience,
            collection_style=collection_style,
        )
        proposed_body = copy.deepcopy(source_revision.canonical_body)
    else:
        if source_template_revision_id not in (None, ''):
            raise QuestionSetError('invalid_source_kind')
        proposed_body = _blank_feedback_body(
            audience=audience,
            collection_style=collection_style,
        )
    body, validation = validate_feedback_body(
        proposed_body,
        audience=audience,
        collection_style=collection_style,
    )
    if _active_question_sets_for_course(course).exists():
        if not confirm_abandon_active:
            raise QuestionSetError('active_draft_exists')
        abandon_active_draft(
            course=course,
            actor=actor,
            instructor_session=instructor_session,
        )
    question_set_values = {
        'course': course,
        'owner': actor,
        'template_id': '',
        'title': body['title'],
        'audience': audience,
        'collection_style': collection_style,
        'source_kind': source_kind,
        'source_template_revision': source_revision,
        'response_unit': QuestionSet.RESPONSE_INDIVIDUAL,
        'aggregation_scope': (
            QuestionSet.AGGREGATION_TEAM
            if audience == QuestionSet.AUDIENCE_TEAM
            else QuestionSet.AGGREGATION_COURSE
        ),
    }
    if question_set_public_id is not None:
        question_set_values['public_id'] = question_set_public_id
    question_set = QuestionSet.objects.create(**question_set_values)
    draft_values = {
        'question_set': question_set,
        'body': body,
        'updated_by': actor,
    }
    if draft_public_id is not None:
        draft_values['public_id'] = draft_public_id
    draft = QuestionSetDraft.objects.create(**draft_values)
    checkpoint = QuestionSetDraftVersion.objects.create(
        question_set=question_set,
        version_number=1,
        canonical_body=body,
        content_hash=hashlib.sha256(_canonical_json(body).encode('utf-8')).hexdigest(),
        author_kind='system',
        author=actor,
        source_template_revision=source_revision,
        change_set=[],
        summary='Copied starting point' if source_revision else 'Started from scratch',
        rationale='',
    )
    draft.current_checkpoint = checkpoint
    draft.save(update_fields=['current_checkpoint', 'updated_at'])
    QuestionSetValidationRun.objects.create(
        draft=draft,
        is_valid=True,
        result=validation,
    )
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_DRAFT_CREATED,
        actor=actor,
        instructor_session=instructor_session,
        question_set=question_set,
        event_id=audit_event_id,
    )
    return draft


@transaction.atomic
def create_draft(
    *,
    course,
    actor,
    instructor_session,
    template_id,
    question_set_public_id=None,
    draft_public_id=None,
    audit_event_id=None,
    confirm_abandon_active=False,
):
    if type(confirm_abandon_active) is not bool:
        raise QuestionSetError('invalid_abandon_confirmation')
    course = Course.objects.select_for_update().get(pk=course.pk)
    _validate_optional_uuid(
        question_set_public_id,
        field_name='question_set_public_id',
        conflict_model=QuestionSet,
    )
    _validate_optional_uuid(
        draft_public_id,
        field_name='draft_public_id',
        conflict_model=QuestionSetDraft,
    )
    template = get_template(template_id)
    body, _result = validate_body(template['body'])
    if _active_question_sets_for_course(course).exists():
        if not confirm_abandon_active:
            raise QuestionSetError('active_draft_exists')
        abandon_active_draft(
            course=course,
            actor=actor,
            instructor_session=instructor_session,
        )
    question_set_values = {
        'course': course,
        'owner': actor,
        'template_id': template['id'],
        'title': body['title'],
        'audience': template['audience'],
    }
    if question_set_public_id is not None:
        question_set_values['public_id'] = question_set_public_id
    question_set = QuestionSet.objects.create(
        **question_set_values,
    )
    draft_values = {
        'question_set': question_set,
        'body': body,
        'updated_by': actor,
    }
    if draft_public_id is not None:
        draft_values['public_id'] = draft_public_id
    draft = QuestionSetDraft.objects.create(**draft_values)
    QuestionSetValidationRun.objects.create(
        draft=draft,
        is_valid=True,
        result={'errors': [], 'question_count': len(body['sections'])},
    )
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_DRAFT_CREATED,
        actor=actor,
        instructor_session=instructor_session,
        question_set=question_set,
        event_id=audit_event_id,
    )
    return draft


@transaction.atomic
def save_draft(
    *,
    draft_id,
    actor,
    instructor_session,
    expected_version,
    body,
    allow_inactive_history=False,
):
    draft = (
        QuestionSetDraft.objects
        .select_for_update(of=('self',))
        .select_related('question_set__course')
        .get(public_id=draft_id)
    )
    question_set = QuestionSet.objects.select_for_update().get(
        pk=draft.question_set_id,
    )
    _require_active_workflow(
        question_set,
        allow_inactive_history=allow_inactive_history,
    )
    if draft.version != expected_version:
        raise QuestionSetError('stale_draft')
    normalized = editable_body(draft.body, body)
    draft.body = normalized
    draft.version += 1
    draft.updated_by = actor
    draft.save(update_fields=['body', 'version', 'updated_by', 'updated_at'])
    question_set.title = normalized['title']
    question_set.save(update_fields=['title', 'updated_at'])
    QuestionSetValidationRun.objects.create(
        draft=draft,
        is_valid=True,
        result={'errors': [], 'question_count': len(normalized['sections'])},
    )
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_DRAFT_SAVED,
        actor=actor,
        instructor_session=instructor_session,
        question_set=question_set,
    )
    return draft


def _feedback_body_for_question_set(question_set, body):
    schema_version = body.get('schema_version') if isinstance(body, dict) else None
    if schema_version in {'guided-feedback-v2', 'open-conversation-v1'}:
        return validate_feedback_body(
            body,
            audience=question_set.audience,
            collection_style=question_set.collection_style,
        )
    normalized = editable_body(body, body)
    return normalized, {
        'errors': [],
        'question_count': len(normalized['sections']),
    }


def _request_hash(payload):
    return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _mutation_receipt(*, scope, idempotency_key, request_hash):
    receipt = QuestionSetMutationReceipt.objects.filter(
        scope=scope,
        idempotency_key=idempotency_key,
    ).first()
    if receipt is not None and receipt.request_hash != request_hash:
        raise QuestionSetError('idempotency_key_conflict')
    return receipt


def _checkpoint_if_needed(
    *,
    draft,
    actor,
    author_kind,
    checkpoint_reason,
    summary,
    rationale='',
    change_set=None,
    restored_from=None,
):
    current = draft.current_checkpoint
    body_hash = hashlib.sha256(_canonical_json(draft.body).encode('utf-8')).hexdigest()
    if current is not None and current.content_hash == body_hash:
        return current
    force = checkpoint_reason in FORCED_CHECKPOINT_REASONS
    interval_elapsed = (
        current is None
        or timezone.now() - current.created_at >= CHECKPOINT_INTERVAL
    )
    if not force and not interval_elapsed:
        return current
    next_number = (
        QuestionSetDraftVersion.objects
        .filter(question_set=draft.question_set)
        .aggregate(value=Max('version_number'))['value']
        or 0
    ) + 1
    checkpoint = QuestionSetDraftVersion.objects.create(
        question_set=draft.question_set,
        version_number=next_number,
        parent_version=current,
        restored_from=restored_from,
        canonical_body=copy.deepcopy(draft.body),
        content_hash=body_hash,
        author_kind=author_kind,
        author=actor,
        source_template_revision=draft.question_set.source_template_revision,
        change_set=change_set or [],
        summary=summary[:240],
        rationale=rationale,
    )
    draft.current_checkpoint = checkpoint
    draft.save(update_fields=['current_checkpoint', 'updated_at'])
    return checkpoint


@transaction.atomic
def save_feedback_draft(
    *,
    draft_id,
    actor,
    instructor_session,
    expected_version,
    body,
    idempotency_key,
    checkpoint_reason=None,
    author_kind='instructor',
    summary='Manual changes',
    rationale='',
    change_set=None,
):
    if checkpoint_reason is not None and checkpoint_reason not in FORCED_CHECKPOINT_REASONS:
        raise QuestionSetError('invalid_checkpoint_reason')
    idempotency_key = _bounded_text(
        idempotency_key,
        'Idempotency key',
        max_length=100,
    )
    if len(idempotency_key) < 8:
        raise QuestionSetError('invalid_idempotency_key')
    request_digest = _request_hash({
        'expected_version': expected_version,
        'body': body,
        'checkpoint_reason': checkpoint_reason,
        'author_kind': author_kind,
    })
    scope = f'draft:{draft_id}'
    existing_receipt = _mutation_receipt(
        scope=scope,
        idempotency_key=idempotency_key,
        request_hash=request_digest,
    )
    draft = (
        QuestionSetDraft.objects
        .select_for_update(of=('self',))
        .select_related(
            'question_set__course',
            'question_set__source_template_revision',
            'current_checkpoint',
        )
        .get(public_id=draft_id)
    )
    if existing_receipt is not None:
        return draft
    question_set = QuestionSet.objects.select_for_update().get(pk=draft.question_set_id)
    _require_active_workflow(question_set)
    if draft.version != expected_version:
        raise QuestionSetError('stale_draft')
    normalized, validation = _feedback_body_for_question_set(question_set, body)
    if _canonical_json(normalized) != _canonical_json(draft.body):
        draft.body = normalized
        draft.version += 1
        draft.updated_by = actor
        draft.save(update_fields=['body', 'version', 'updated_by', 'updated_at'])
        question_set.title = normalized['title']
        question_set.save(update_fields=['title', 'updated_at'])
        QuestionSetValidationRun.objects.create(
            draft=draft,
            is_valid=True,
            result=validation,
        )
    checkpoint = _checkpoint_if_needed(
        draft=draft,
        actor=actor,
        author_kind=author_kind,
        checkpoint_reason=checkpoint_reason,
        summary=summary,
        rationale=rationale,
        change_set=change_set,
    )
    QuestionSetMutationReceipt.objects.create(
        scope=scope,
        operation_kind='draft_save',
        idempotency_key=idempotency_key,
        request_hash=request_digest,
        result_version=(
            checkpoint
            if checkpoint is not None and checkpoint.content_hash == hashlib.sha256(
                _canonical_json(draft.body).encode('utf-8')
            ).hexdigest()
            else None
        ),
        result_draft_version=draft.version,
    )
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_DRAFT_SAVED,
        actor=actor,
        instructor_session=instructor_session,
        question_set=question_set,
    )
    return draft


def serialize_draft_versions(question_set):
    return [
        {
            'id': str(version.public_id),
            'version_number': version.version_number,
            'author_kind': version.author_kind,
            'summary': version.summary,
            'rationale': version.rationale,
            'restored_from_id': (
                str(version.restored_from.public_id)
                if version.restored_from_id else None
            ),
            'created_at': version.created_at.isoformat(),
        }
        for version in question_set.draft_versions.select_related(
            'restored_from',
        ).order_by('-version_number')
    ]


def serialize_draft_version(version):
    return {
        **next(
            row for row in serialize_draft_versions(version.question_set)
            if row['id'] == str(version.public_id)
        ),
        'canonical_body': copy.deepcopy(version.canonical_body),
        'change_set': copy.deepcopy(version.change_set),
    }


@transaction.atomic
def restore_feedback_draft(
    *,
    draft_id,
    actor,
    instructor_session,
    expected_version,
    version_id,
    idempotency_key,
):
    idempotency_key = _bounded_text(
        idempotency_key,
        'Idempotency key',
        max_length=100,
    )
    request_digest = _request_hash({
        'expected_version': expected_version,
        'version_id': str(version_id),
    })
    scope = f'draft:{draft_id}'
    existing_receipt = _mutation_receipt(
        scope=scope,
        idempotency_key=idempotency_key,
        request_hash=request_digest,
    )
    draft = (
        QuestionSetDraft.objects
        .select_for_update(of=('self',))
        .select_related(
            'question_set__course',
            'question_set__source_template_revision',
            'current_checkpoint',
        )
        .get(public_id=draft_id)
    )
    if existing_receipt is not None:
        return draft
    question_set = QuestionSet.objects.select_for_update().get(pk=draft.question_set_id)
    _require_active_workflow(question_set)
    if draft.version != expected_version:
        raise QuestionSetError('stale_draft')
    try:
        target = QuestionSetDraftVersion.objects.get(
            public_id=version_id,
            question_set=question_set,
        )
    except (QuestionSetDraftVersion.DoesNotExist, ValueError):
        raise QuestionSetError('draft_version_not_found')
    normalized, validation = _feedback_body_for_question_set(
        question_set,
        target.canonical_body,
    )
    draft.body = normalized
    draft.version += 1
    draft.updated_by = actor
    draft.save(update_fields=['body', 'version', 'updated_by', 'updated_at'])
    question_set.title = normalized['title']
    question_set.save(update_fields=['title', 'updated_at'])
    QuestionSetValidationRun.objects.create(
        draft=draft,
        is_valid=True,
        result=validation,
    )
    checkpoint = _checkpoint_if_needed(
        draft=draft,
        actor=actor,
        author_kind='restore',
        checkpoint_reason='restore',
        summary=f'Restored Version {target.version_number}',
        restored_from=target,
    )
    QuestionSetMutationReceipt.objects.create(
        scope=scope,
        operation_kind='draft_restore',
        idempotency_key=idempotency_key,
        request_hash=request_digest,
        result_version=checkpoint,
        result_draft_version=draft.version,
    )
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_DRAFT_SAVED,
        actor=actor,
        instructor_session=instructor_session,
        question_set=question_set,
    )
    return draft


def _canonical_json(body):
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _normalized_preview_text(value):
    return ' '.join(str(value or '').split()).casefold()


def _compile_feedback_body(canonical, question_set, revision_number):
    schema_id = f'question-set:{question_set.public_id}:v{revision_number}'
    schema_version = canonical.get('schema_version')
    if schema_version == 'guided-feedback-v2':
        compiled_sections = []
        for section in canonical['sections']:
            for question in section['questions']:
                follow_up = question['follow_up']
                compiled_sections.append({
                    'id': question['id'],
                    'source_section_id': section['id'],
                    'title': question['short_label'],
                    'topic': section['title'].lower(),
                    'one_line': question['short_label'].lower(),
                    'opening_prompt': question['prompt'],
                    'depth_probe': (
                        follow_up['prompt'] if follow_up['enabled'] else None
                    ),
                    'fields': [{
                        'id': question['id'],
                        'kind': 'longform',
                        'label': question['prompt'],
                    }],
                })
        return {
            'schema_id': schema_id,
            'version': str(revision_number),
            'schema_version': schema_version,
            'title': canonical['title'],
            'intro': canonical['intro'],
            'transition_template': 'Thanks. Is there anything else before we move on?',
            'advance_template': 'Got it. Next: {{next_section_title}}.',
            'shallow_word_threshold': 25,
            'max_probes_per_section': 1,
            'sections': compiled_sections,
            'closing': {
                'behavior': (
                    'After every question has a response, ask the closing question '
                    'and then emit [END] on its own line.'
                ),
                'feedback_prompt': canonical['closing']['prompt'],
            },
            'ordering_rules': {
                'strict_in_order': True,
                'must_cover_all_sections': True,
                'stop_warn_then_honor': True,
            },
            'audience': question_set.audience,
            'response_unit': question_set.response_unit,
            'aggregation_scope': question_set.aggregation_scope,
            'effective_settings': copy.deepcopy(QUESTION_SET_EFFECTIVE_SETTINGS),
        }
    if schema_version == 'open-conversation-v1':
        return {
            **copy.deepcopy(canonical),
            'schema_id': schema_id,
            'version': str(revision_number),
            'audience': question_set.audience,
            'response_unit': question_set.response_unit,
            'aggregation_scope': question_set.aggregation_scope,
            'engine_policy': {
                'follow_student_topics': True,
                'stay_within_listening_goal': True,
                'honor_stop': True,
            },
            'effective_settings': copy.deepcopy(QUESTION_SET_EFFECTIVE_SETTINGS),
        }
    compiled = copy.deepcopy(canonical)
    compiled['schema_id'] = schema_id
    compiled['version'] = str(revision_number)
    compiled['effective_settings'] = copy.deepcopy(QUESTION_SET_EFFECTIVE_SETTINGS)
    return compiled


@transaction.atomic
def freeze_draft(
    *,
    draft_id,
    actor,
    instructor_session,
    expected_version,
    revision_public_id=None,
    audit_event_id=None,
    allow_inactive_history=False,
):
    _validate_optional_uuid(
        revision_public_id,
        field_name='revision_public_id',
    )
    draft = (
        QuestionSetDraft.objects
        .select_for_update()
        .select_related('question_set__course')
        .get(public_id=draft_id)
    )
    question_set = QuestionSet.objects.select_for_update().get(
        pk=draft.question_set_id,
    )
    _require_active_workflow(
        question_set,
        allow_inactive_history=allow_inactive_history,
    )
    if draft.version != expected_version:
        raise QuestionSetError('stale_draft')
    canonical, validation = _feedback_body_for_question_set(question_set, draft.body)
    checkpoint = _checkpoint_if_needed(
        draft=draft,
        actor=actor,
        author_kind='instructor',
        checkpoint_reason='preview',
        summary='Ready for preview',
    )
    protocol_schema_version = canonical.get('schema_version', 'question-set-v1')
    engine_version = (
        'open-conversation-v1'
        if protocol_schema_version == 'open-conversation-v1'
        else ENGINE_VERSION
    )
    revision_identity = {
        'canonical_body': canonical,
        'compiler_version': COMPILER_VERSION,
        'engine_version': engine_version,
        'protocol_schema_version': protocol_schema_version,
        'audience': question_set.audience,
        'collection_style': question_set.collection_style,
    }
    content_hash = hashlib.sha256(
        _canonical_json(revision_identity).encode('utf-8')
    ).hexdigest()
    existing = QuestionSetRevision.objects.filter(
        question_set=question_set,
        content_hash=content_hash,
    ).first()
    if existing is not None:
        if (
            revision_public_id is not None
            and existing.public_id != revision_public_id
        ):
            raise QuestionSetError('revision_public_id_conflict')
        return existing, False

    if (
        revision_public_id is not None
        and QuestionSetRevision.objects.filter(public_id=revision_public_id).exists()
    ):
        raise QuestionSetError('revision_public_id_conflict')

    latest_number = (
        QuestionSetRevision.objects
        .filter(question_set=question_set)
        .aggregate(value=Max('revision_number'))['value']
        or 0
    )
    revision_number = latest_number + 1
    compiled = _compile_feedback_body(canonical, question_set, revision_number)
    revision_values = {
        'question_set': question_set,
        'revision_number': revision_number,
        'source_draft_version': draft.version,
        'source_checkpoint': checkpoint,
        'canonical_body': canonical,
        'compiled_protocol': compiled,
        'content_hash': content_hash,
        'compiler_version': COMPILER_VERSION,
        'engine_version': engine_version,
        'author_kind': 'instructor',
        'protocol_schema_version': protocol_schema_version,
        'created_by': actor,
    }
    if revision_public_id is not None:
        revision_values['public_id'] = revision_public_id
    revision = QuestionSetRevision.objects.create(
        **revision_values,
    )
    QuestionSetValidationRun.objects.create(
        revision=revision,
        is_valid=True,
        result=validation,
    )
    draft.base_revision = revision
    draft.save(update_fields=['base_revision', 'updated_at'])
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_REVISION_FROZEN,
        actor=actor,
        instructor_session=instructor_session,
        question_set=question_set,
        event_id=audit_event_id,
    )
    return revision, True


@transaction.atomic
def issue_preview_capability(
    *,
    revision,
    actor,
    instructor_session,
    preview_public_id=None,
    audit_event_id=None,
    allow_inactive_history=False,
):
    _validate_optional_uuid(
        preview_public_id,
        field_name='preview_public_id',
        conflict_model=PreviewSession,
    )
    question_set = QuestionSet.objects.select_for_update().get(
        pk=revision.question_set_id,
    )
    _require_active_workflow(
        question_set,
        allow_inactive_history=allow_inactive_history,
    )
    created_at = timezone.now()
    ready_delay_ms = PREVIEW_READY_DELAY_MIN_MS + secrets.randbelow(
        PREVIEW_READY_DELAY_MAX_MS - PREVIEW_READY_DELAY_MIN_MS + 1,
    )
    raw_token = secrets.token_urlsafe(32)
    preview_values = {
        'revision': revision,
        'instructor': actor,
        'token_digest': token_digest(raw_token),
        'expires_at': created_at + PREVIEW_TTL,
        'ready_at': created_at + timedelta(milliseconds=ready_delay_ms),
    }
    if preview_public_id is not None:
        preview_values['public_id'] = preview_public_id
    preview = PreviewSession.objects.create(**preview_values)
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_STARTED,
        actor=actor,
        instructor_session=instructor_session,
        question_set=question_set,
        event_id=audit_event_id,
    )
    return raw_token, preview


def get_preview(raw_token):
    preview = (
        PreviewSession.objects
        .select_related('revision__question_set__course', 'instructor')
        .filter(token_digest=token_digest(raw_token))
        .first()
    )
    if preview is None:
        raise QuestionSetError('preview_not_found')
    _require_preview_available(preview, require_ready=True)
    return preview


def _require_preview_available(preview, *, require_ready):
    now = timezone.now()
    if preview.expires_at <= now:
        raise QuestionSetError('preview_expired')
    if require_ready and preview.ready_at > now:
        retry_after_ms = max(
            1,
            math.ceil((preview.ready_at - now).total_seconds() * 1000),
        )
        raise QuestionSetError(
            'preview_preparing',
            timing={
                'ready_at': preview.ready_at.isoformat(),
                'retry_after_ms': retry_after_ms,
            },
        )


def _preview_for_settings(raw_token):
    preview = (
        PreviewSession.objects
        .select_related('revision__question_set__course', 'instructor')
        .filter(token_digest=token_digest(raw_token))
        .first()
    )
    if preview is None:
        raise QuestionSetError('preview_not_found')
    _require_preview_available(preview, require_ready=False)
    return preview


def serialize_preview_settings(preview):
    return {
        'completion_certificate_enabled': preview.completion_certificate_enabled,
        'parsed_document_download_enabled': preview.parsed_document_download_enabled,
    }


def serialize_preview_status(preview):
    return {
        'preview_completed': preview.completed_at is not None,
        'preview_skipped': preview.skipped_at is not None,
    }


@transaction.atomic
def update_preview_settings(*, raw_token, actor, instructor_session, settings):
    if not isinstance(settings, dict):
        raise QuestionSetError('invalid_preview_settings')
    allowed = {
        'completion_certificate_enabled',
        'parsed_document_download_enabled',
    }
    if not settings or not set(settings).issubset(allowed):
        raise QuestionSetError('invalid_preview_settings')
    if any(type(value) is not bool for value in settings.values()):
        raise QuestionSetError('invalid_preview_settings')

    preview = _preview_for_settings(raw_token)
    preview = (
        PreviewSession.objects
        .select_for_update()
        .select_related('revision__question_set__course', 'instructor')
        .get(pk=preview.pk)
    )
    _require_preview_available(preview, require_ready=False)
    if preview.instructor_id != actor.pk:
        raise QuestionSetError('preview_owner_mismatch')
    if preview.survey_links.exists():
        raise QuestionSetError('preview_published')
    for field_name, value in settings.items():
        setattr(preview, field_name, value)
    preview.save(update_fields=[*settings.keys()])
    record_instructor_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_SETTINGS_UPDATED,
        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        actor=actor,
        session=instructor_session,
        course=preview.revision.question_set.course,
        target_type='question_set',
        target_id=preview.revision.question_set.public_id,
        metadata=serialize_preview_settings(preview),
    )
    return preview


@transaction.atomic
def save_preview_message(*, raw_token, role, content, attribution=None):
    preview = get_preview(raw_token)
    preview = PreviewSession.objects.select_for_update().get(pk=preview.pk)
    if role not in {PreviewMessage.ROLE_USER, PreviewMessage.ROLE_ASSISTANT}:
        raise QuestionSetError('invalid_preview_message', 'Role is not allowed.')
    content = _bounded_text(content, 'Message')
    if attribution is None:
        attribution = {}
    if not isinstance(attribution, dict):
        raise QuestionSetError('invalid_preview_message', 'Attribution must be an object.')
    sequence = preview.next_message_sequence
    message = PreviewMessage.objects.create(
        preview_session=preview,
        sequence=sequence,
        role=role,
        content=content,
        attribution=attribution,
    )
    preview.next_message_sequence = sequence + 1
    preview.save(update_fields=['next_message_sequence'])
    return message


@transaction.atomic
def complete_preview(*, raw_token, audit_event_id=None):
    preview = get_preview(raw_token)
    preview = (
        PreviewSession.objects
        .select_for_update()
        .select_related('revision__question_set__course', 'instructor')
        .get(pk=preview.pk)
    )
    protocol = preview.revision.compiled_protocol
    if protocol.get('schema_version') == 'open-conversation-v1':
        messages = list(preview.messages.order_by('sequence', 'id'))
        closing_prompt = _normalized_preview_text(protocol.get('closing_prompt'))
        closing_question = next((
            message for message in messages
            if message.role == PreviewMessage.ROLE_ASSISTANT
            and closing_prompt
            and closing_prompt in _normalized_preview_text(message.content)
        ), None)
        closing_answer = next((
            message for message in messages
            if closing_question is not None
            and message.sequence > closing_question.sequence
            and message.role == PreviewMessage.ROLE_USER
        ), None)
        final_assistant = next((
            message for message in messages
            if closing_answer is not None
            and message.sequence > closing_answer.sequence
            and message.role == PreviewMessage.ROLE_ASSISTANT
        ), None)
        if closing_question is None or closing_answer is None or final_assistant is None:
            raise QuestionSetError(
                'preview_incomplete',
                'Complete the open conversation before continuing.',
            )
        if preview.completed_at is None:
            preview.completed_at = timezone.now()
            preview.save(update_fields=['completed_at'])
            _record_question_set_event(
                action=InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_COMPLETED,
                actor=preview.instructor,
                instructor_session=None,
                question_set=preview.revision.question_set,
                event_id=audit_event_id,
            )
        return preview
    required_fields = [
        (
            str(section['id']),
            str(field['id']),
            str(field.get('label') or section.get('opening_prompt') or ''),
        )
        for section in protocol.get('sections', [])
        for field in section.get('fields', [])
    ]
    schema_id = str(protocol.get('schema_id') or '')
    schema_version = str(protocol.get('version') or '')
    messages = list(preview.messages.order_by('sequence', 'id'))
    cursor = 0
    valid_walk = bool(required_fields)
    for section_id, field_id, prompt in required_fields:
        expected_prompt = _normalized_preview_text(prompt)

        asked = next((
            message for message in messages
            if message.sequence > cursor
            and message.role == PreviewMessage.ROLE_ASSISTANT
            and str((message.attribution or {}).get('form_schema_id') or '') == schema_id
            and str((message.attribution or {}).get('form_schema_version') or '') == schema_version
            and str((message.attribution or {}).get('form_section_id') or '') == section_id
            and str((message.attribution or {}).get('form_field_id') or '') == field_id
            and expected_prompt
            and expected_prompt in _normalized_preview_text(message.content)
        ), None)
        if asked is None:
            valid_walk = False
            break

        answered = next((
            message for message in messages
            if message.sequence > asked.sequence
            and message.role == PreviewMessage.ROLE_USER
            and str((message.attribution or {}).get('form_schema_id') or '') == schema_id
            and str((message.attribution or {}).get('form_schema_version') or '') == schema_version
            and str((message.attribution or {}).get('form_section_id') or '') == section_id
            and str((message.attribution or {}).get('form_field_id') or '') == field_id
        ), None)
        if answered is None:
            valid_walk = False
            break
        cursor = answered.sequence

    closing_prompt = _normalized_preview_text(
        (protocol.get('closing') or {}).get('feedback_prompt')
    )
    closing_question = next((
        message for message in messages
        if valid_walk
        and message.sequence > cursor
        and message.role == PreviewMessage.ROLE_ASSISTANT
        and not (message.attribution or {}).get('form_field_id')
        and closing_prompt
        and closing_prompt in _normalized_preview_text(message.content)
    ), None)
    closing_answer = next((
        message for message in messages
        if closing_question is not None
        and message.sequence > closing_question.sequence
        and message.role == PreviewMessage.ROLE_USER
        and not (message.attribution or {}).get('form_field_id')
    ), None)
    final_ack = next((
        message for message in messages
        if closing_answer is not None
        and message.sequence > closing_answer.sequence
        and message.role == PreviewMessage.ROLE_ASSISTANT
    ), None)
    if not valid_walk or closing_question is None or closing_answer is None or final_ack is None:
        raise QuestionSetError(
            'preview_incomplete',
            'Answer every question and reach the closing before completing the preview.',
        )
    if preview.completed_at is None:
        preview.completed_at = timezone.now()
        preview.save(update_fields=['completed_at'])
        _record_question_set_event(
            action=InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_COMPLETED,
            actor=preview.instructor,
            instructor_session=None,
            question_set=preview.revision.question_set,
            event_id=audit_event_id,
        )
    return preview


@transaction.atomic
def skip_preview(*, raw_token, actor, instructor_session, acknowledge_warning):
    preview = get_preview(raw_token)
    preview = (
        PreviewSession.objects
        .select_for_update()
        .select_related('revision__question_set__course', 'instructor')
        .get(pk=preview.pk)
    )
    _require_preview_available(preview, require_ready=True)
    if preview.instructor_id != actor.pk:
        raise QuestionSetError('preview_owner_mismatch')
    if preview.survey_links.exists():
        raise QuestionSetError('preview_published')
    if preview.completed_at is not None:
        return preview

    previously_confirmed = InstructorAuditEvent.objects.filter(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_SKIPPED,
        actor=actor,
        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
    ).exists()
    if not previously_confirmed and acknowledge_warning is not True:
        raise QuestionSetError(
            'preview_skip_confirmation_required',
            'Confirm that you want to skip the student preview.',
        )
    if preview.skipped_at is None:
        preview.skipped_at = timezone.now()
        preview.save(update_fields=['skipped_at'])
        _record_question_set_event(
            action=InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_SKIPPED,
            actor=actor,
            instructor_session=instructor_session,
            question_set=preview.revision.question_set,
        )
    return preview


def _new_survey_public_id():
    alphabet = string.ascii_lowercase + string.digits
    for _attempt in range(20):
        public_id = ''.join(secrets.choice(alphabet) for _ in range(10))
        if not FeedbackGPT.objects.filter(public_id=public_id).exists():
            return public_id
    raise QuestionSetError('public_id_generation_failed')


def _validate_existing_survey_retry(
    *,
    link,
    revision,
    actor,
    instructor_session,
    preview,
    survey_public_id,
    audit_event_id,
    team_configuration=None,
):
    if (
        link.revision_id != revision.pk
        or link.created_by_id != actor.pk
        or link.preview_session_id != preview.pk
        or link.completion_certificate_enabled != preview.completion_certificate_enabled
        or link.parsed_document_download_enabled != preview.parsed_document_download_enabled
        or link.team_configuration_id != getattr(team_configuration, 'pk', None)
    ):
        raise QuestionSetError('idempotency_key_conflict')
    if (
        survey_public_id is not None
        and link.survey.public_id != survey_public_id
    ):
        raise QuestionSetError('survey_public_id_conflict')
    if audit_event_id is None:
        return
    event = InstructorAuditEvent.objects.filter(event_id=audit_event_id).first()
    if event is None:
        raise QuestionSetError('audit_event_id_not_found')
    course = revision.question_set.course
    if (
        event.action != InstructorAuditEvent.ACTION_SURVEY_CREATED
        or event.outcome != InstructorAuditEvent.OUTCOME_SUCCESS
        or event.actor_id != actor.pk
        or event.session_id != getattr(instructor_session, 'pk', None)
        or event.course_id != course.pk
        or event.course_id_snapshot != course.course_id
        or event.target_type != 'survey'
        or event.target_id != str(link.survey.pk)
        or event.metadata != {'mode': link.survey.mode}
    ):
        raise QuestionSetError('audit_event_id_conflict')


@transaction.atomic
def create_survey_from_revision(
    *,
    revision,
    actor,
    instructor_session,
    idempotency_key,
    survey_label,
    week_number,
    opens_at,
    expires_at,
    preview_token,
    survey_public_id=None,
    audit_event_id=None,
    team_configuration=None,
):
    if not isinstance(preview_token, str) or not preview_token:
        raise QuestionSetError('preview_token_required')
    preview = get_preview(preview_token)
    preview = (
        PreviewSession.objects
        .select_for_update()
        .select_related('revision__question_set__course', 'instructor')
        .get(pk=preview.pk)
    )
    _require_preview_available(preview, require_ready=True)
    if preview.instructor_id != actor.pk:
        raise QuestionSetError('preview_owner_mismatch')
    if preview.revision_id != revision.pk:
        raise QuestionSetError('preview_revision_mismatch')
    if preview.completed_at is None and preview.skipped_at is None:
        raise QuestionSetError('preview_incomplete')
    idempotency_key = _bounded_text(
        idempotency_key,
        'Idempotency key',
        max_length=100,
    )
    if len(idempotency_key) < 8:
        raise QuestionSetError('invalid_idempotency_key')
    if survey_public_id is not None:
        if type(survey_public_id) is not str:
            raise QuestionSetError(
                'invalid_survey_public_id',
                'Survey public ID must be text.',
            )
        survey_public_id = survey_public_id.strip()
        if not survey_public_id or len(survey_public_id) > 16:
            raise QuestionSetError(
                'invalid_survey_public_id',
                'Survey public ID must be between 1 and 16 characters.',
            )
    if audit_event_id is not None and type(audit_event_id) is not uuid.UUID:
        raise QuestionSetError(
            'invalid_audit_event_id',
            'Audit event ID must be a UUID.',
        )
    existing = (
        QuestionSetSurvey.objects
        .select_related('survey', 'revision')
        .filter(idempotency_key=idempotency_key)
        .first()
    )
    if existing is not None:
        _validate_existing_survey_retry(
            link=existing,
            revision=revision,
            actor=actor,
            instructor_session=instructor_session,
            preview=preview,
            survey_public_id=survey_public_id,
            audit_event_id=audit_event_id,
            team_configuration=team_configuration,
        )
        return existing, False
    question_set = QuestionSet.objects.select_for_update().get(
        pk=revision.question_set_id,
    )
    existing = (
        QuestionSetSurvey.objects
        .select_related('survey', 'revision')
        .filter(idempotency_key=idempotency_key)
        .first()
    )
    if existing is not None:
        _validate_existing_survey_retry(
            link=existing,
            revision=revision,
            actor=actor,
            instructor_session=instructor_session,
            preview=preview,
            survey_public_id=survey_public_id,
            audit_event_id=audit_event_id,
            team_configuration=team_configuration,
        )
        return existing, False
    _require_active_workflow(question_set)
    if week_number is not None and (
        type(week_number) is not int or not 1 <= week_number <= 99
    ):
        raise QuestionSetError('invalid_week_number')
    survey_label = _bounded_text(
        survey_label or revision.compiled_protocol['title'],
        'Survey label',
        max_length=200,
    )
    if opens_at is not None and expires_at is not None and opens_at >= expires_at:
        raise QuestionSetError('invalid_schedule', 'Closing time must be after opening time.')

    if question_set.audience == QuestionSet.AUDIENCE_TEAM:
        if team_configuration is None:
            raise QuestionSetError('team_configuration_required')
        if (
            not isinstance(team_configuration, TeamConfiguration)
            or team_configuration.course_id != question_set.course_id
            or team_configuration.archived
            or not team_configuration.teams.exists()
        ):
            raise QuestionSetError('invalid_team_configuration')
        survey_mode = 'group'
    elif question_set.collection_style == QuestionSet.COLLECTION_OPEN:
        if team_configuration is not None:
            raise QuestionSetError('invalid_team_configuration')
        survey_mode = 'general'
    else:
        if team_configuration is not None:
            raise QuestionSetError('invalid_team_configuration')
        survey_mode = 'form'

    if survey_mode == 'general':
        protocol = revision.compiled_protocol
        instructions = (
            'You are LEAI, a supportive course-feedback facilitator. Begin with: '
            f'"{protocol["opening_prompt"]}" Listen for: {protocol["listening_goal"]} '
            f'Before ending, ask: "{protocol["closing_prompt"]}" After the student '
            'answers the closing question, acknowledge them and emit [END] on its own line.'
        )
    else:
        instructions = (
            'You are LEAI, a conversational feedback facilitator. Follow the '
            'provided guided questions exactly, ask one question at a time, '
            'and keep your responses concise and supportive.'
        )

    if (
        survey_public_id is not None
        and FeedbackGPT.objects.filter(public_id=survey_public_id).exists()
    ):
        raise QuestionSetError('survey_public_id_conflict')

    survey = FeedbackGPT.objects.create(
        public_id=survey_public_id or _new_survey_public_id(),
        name=survey_label,
        survey_label=survey_label,
        instructions=instructions,
        created_by=actor.display_name,
        course=revision.question_set.course,
        week_number=week_number,
        opens_at=opens_at,
        expires_at=expires_at,
        is_closed=False,
        anonymity_mode='anonymous',
        reporting_structure='',
        mode=survey_mode,
        form_schema=None,
    )
    try:
        with transaction.atomic():
            link = QuestionSetSurvey.objects.create(
                survey=survey,
                revision=revision,
                preview_session=preview,
                completion_certificate_enabled=preview.completion_certificate_enabled,
                parsed_document_download_enabled=preview.parsed_document_download_enabled,
                team_configuration=team_configuration,
                publication_manifest={
                    'revision_id': str(revision.public_id),
                    'mode': survey_mode,
                    'audience': question_set.audience,
                    'collection_style': question_set.collection_style,
                    'anonymity_mode': 'anonymous',
                    'completion_certificate_enabled': preview.completion_certificate_enabled,
                    'parsed_document_download_enabled': preview.parsed_document_download_enabled,
                    'opens_at': opens_at.isoformat() if opens_at else None,
                    'expires_at': expires_at.isoformat() if expires_at else None,
                    'team_configuration_id': team_configuration.pk if team_configuration else None,
                    'team_selection': 'self_selected' if team_configuration else None,
                },
                idempotency_key=idempotency_key,
                created_by=actor,
            )
            if team_configuration is not None:
                snapshot = SurveyTeamSnapshot.objects.create(
                    survey=survey,
                    source_configuration=team_configuration,
                    name=team_configuration.name,
                    label_prefix=team_configuration.label_prefix,
                    color=team_configuration.color,
                )
                for team in team_configuration.teams.all():
                    SurveyTeam.objects.create(
                        snapshot=snapshot,
                        number=team.number,
                        size=team.size,
                        display_name=team.display_name,
                    )
    except IntegrityError:
        survey.delete()
        winner = (
            QuestionSetSurvey.objects
            .select_related('survey', 'revision')
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if winner is None:
            raise
        _validate_existing_survey_retry(
            link=winner,
            revision=revision,
            actor=actor,
            instructor_session=instructor_session,
            preview=preview,
            survey_public_id=survey_public_id,
            audit_event_id=audit_event_id,
            team_configuration=team_configuration,
        )
        return winner, False
    record_instructor_event(
        event_id=audit_event_id,
        action=InstructorAuditEvent.ACTION_SURVEY_CREATED,
        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        actor=actor,
        session=instructor_session,
        course=revision.question_set.course,
        target_type='survey',
        target_id=survey.pk,
        metadata={'mode': survey_mode},
    )
    question_set.workflow_status = QuestionSet.WORKFLOW_COMPLETED
    question_set.save(update_fields=['workflow_status', 'updated_at'])
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_WORKFLOW_COMPLETED,
        actor=actor,
        instructor_session=instructor_session,
        question_set=question_set,
    )
    return link, True


def serialize_survey_link(link):
    survey = link.survey
    return {
        'id': survey.pk,
        'public_id': survey.public_id,
        'name': survey.name,
        'survey_label': survey.survey_label,
        'mode': survey.mode,
        'question_set_revision_id': str(link.revision.public_id),
        'direct_url': f'feedback.html?id={survey.public_id}',
        'opens_at': survey.opens_at.isoformat() if survey.opens_at else None,
        'expires_at': survey.expires_at.isoformat() if survey.expires_at else None,
        'completion_certificate_enabled': link.completion_certificate_enabled,
        'parsed_document_download_enabled': link.parsed_document_download_enabled,
        'team_configuration_id': link.team_configuration_id,
    }


def _template_body_hash(body):
    return hashlib.sha256(_canonical_json(body).encode('utf-8')).hexdigest()


@transaction.atomic
def save_private_template(
    *,
    revision,
    actor,
    name,
    description='',
):
    question_set = revision.question_set
    if question_set.owner_id != actor.pk:
        raise QuestionSetError('template_access_denied')
    name = _bounded_text(name, 'Template name', max_length=200)
    description = _bounded_text(
        description or '',
        'Template description',
        required=False,
        max_length=2000,
    )
    parent_revision = question_set.source_template_revision
    origin_revision = None
    if parent_revision is not None:
        origin_revision = parent_revision.template.origin_revision or parent_revision
    template = QuestionSetTemplate.objects.create(
        name=name,
        description=description,
        scope=QuestionSetTemplate.SCOPE_INSTRUCTOR,
        owner=actor,
        institution=question_set.course.institution,
        audience=question_set.audience,
        collection_style=question_set.collection_style,
        visibility=QuestionSetTemplate.VISIBILITY_PRIVATE,
        forked_from_revision=parent_revision,
        origin_revision=origin_revision,
    )
    template_revision = QuestionSetTemplateRevision.objects.create(
        template=template,
        revision_number=1,
        canonical_body=copy.deepcopy(revision.canonical_body),
        content_hash=_template_body_hash(revision.canonical_body),
        protocol_schema_version=revision.protocol_schema_version,
        created_by=actor,
        source_question_set_revision=revision,
        source_checkpoint=revision.source_checkpoint,
        provenance='instructor_saved',
    )
    if template.origin_revision_id is None:
        template.origin_revision = template_revision
        template.save(update_fields=['origin_revision', 'updated_at'])
    return template, template_revision


@transaction.atomic
def publish_template_to_community(*, template, revision, actor):
    template = QuestionSetTemplate.objects.select_for_update().get(pk=template.pk)
    if template.owner_id != actor.pk or revision.template_id != template.pk:
        raise QuestionSetError('template_access_denied')
    template.visibility = QuestionSetTemplate.VISIBILITY_COMMUNITY
    template.community_revision = revision
    template.community_published_at = timezone.now()
    template.community_withdrawn_at = None
    template.save(update_fields=[
        'visibility', 'community_revision', 'community_published_at',
        'community_withdrawn_at', 'updated_at',
    ])
    return template


@transaction.atomic
def withdraw_template_from_community(*, template, actor):
    template = QuestionSetTemplate.objects.select_for_update().get(pk=template.pk)
    if template.owner_id != actor.pk:
        raise QuestionSetError('template_access_denied')
    template.visibility = QuestionSetTemplate.VISIBILITY_PRIVATE
    template.community_revision = None
    template.community_withdrawn_at = timezone.now()
    template.save(update_fields=[
        'visibility', 'community_revision', 'community_withdrawn_at', 'updated_at',
    ])
    return template


def serialize_template(template, revision=None):
    if revision is None:
        revision = template.community_revision or template.revisions.order_by(
            '-revision_number', '-id',
        ).first()
    return {
        'id': str(template.public_id),
        'name': template.name,
        'description': template.description,
        'audience': template.audience,
        'collection_style': template.collection_style,
        'visibility': template.visibility,
        'revision_id': str(revision.public_id) if revision else None,
        'revision_number': revision.revision_number if revision else None,
        'forked_from_revision_id': (
            str(template.forked_from_revision.public_id)
            if template.forked_from_revision_id else None
        ),
        'origin_revision_id': (
            str(template.origin_revision.public_id)
            if template.origin_revision_id else None
        ),
    }
