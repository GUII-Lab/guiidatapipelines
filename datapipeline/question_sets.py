from __future__ import annotations

import copy
import hashlib
import json
import secrets
import string
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from .instructor_audit import record_instructor_event
from .instructor_auth import token_digest
from .models import (
    FeedbackGPT,
    InstructorAuditEvent,
    PreviewMessage,
    PreviewSession,
    QuestionSet,
    QuestionSetDraft,
    QuestionSetRevision,
    QuestionSetSurvey,
    QuestionSetValidationRun,
)


COMPILER_VERSION = '1'
ENGINE_VERSION = 'formmode-v1'
PREVIEW_TTL = timedelta(hours=12)
MAX_TEXT_LENGTH = 4000

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
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def list_templates():
    return copy.deepcopy(list(SYSTEM_TEMPLATES))


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
        'body': copy.deepcopy(draft.body),
        'version': draft.version,
        'base_revision_id': (
            str(draft.base_revision.public_id) if draft.base_revision_id else None
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


def _record_question_set_event(*, action, actor, instructor_session, question_set):
    record_instructor_event(
        action=action,
        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        actor=actor,
        session=instructor_session,
        course=question_set.course,
        target_type='question_set',
        target_id=question_set.public_id,
        metadata={},
    )


@transaction.atomic
def create_draft(*, course, actor, instructor_session, template_id):
    template = get_template(template_id)
    body, _result = validate_body(template['body'])
    question_set = QuestionSet.objects.create(
        course=course,
        owner=actor,
        template_id=template['id'],
        title=body['title'],
        audience=template['audience'],
    )
    draft = QuestionSetDraft.objects.create(
        question_set=question_set,
        body=body,
        updated_by=actor,
    )
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
    )
    return draft


@transaction.atomic
def save_draft(*, draft_id, actor, instructor_session, expected_version, body):
    draft = (
        QuestionSetDraft.objects
        .select_for_update()
        .select_related('question_set__course')
        .get(public_id=draft_id)
    )
    if draft.version != expected_version:
        raise QuestionSetError('stale_draft')
    normalized = editable_body(draft.body, body)
    draft.body = normalized
    draft.version += 1
    draft.updated_by = actor
    draft.save(update_fields=['body', 'version', 'updated_by', 'updated_at'])
    draft.question_set.title = normalized['title']
    draft.question_set.save(update_fields=['title', 'updated_at'])
    QuestionSetValidationRun.objects.create(
        draft=draft,
        is_valid=True,
        result={'errors': [], 'question_count': len(normalized['sections'])},
    )
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_DRAFT_SAVED,
        actor=actor,
        instructor_session=instructor_session,
        question_set=draft.question_set,
    )
    return draft


def _canonical_json(body):
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _normalized_preview_text(value):
    return ' '.join(str(value or '').split()).casefold()


@transaction.atomic
def freeze_draft(*, draft_id, actor, instructor_session, expected_version):
    draft = (
        QuestionSetDraft.objects
        .select_for_update()
        .select_related('question_set__course')
        .get(public_id=draft_id)
    )
    if draft.version != expected_version:
        raise QuestionSetError('stale_draft')
    canonical, validation = validate_body(draft.body)
    revision_identity = {
        'canonical_body': canonical,
        'compiler_version': COMPILER_VERSION,
        'engine_version': ENGINE_VERSION,
    }
    content_hash = hashlib.sha256(
        _canonical_json(revision_identity).encode('utf-8')
    ).hexdigest()
    existing = QuestionSetRevision.objects.filter(
        question_set=draft.question_set,
        content_hash=content_hash,
    ).first()
    if existing is not None:
        return existing, False

    latest_number = (
        QuestionSetRevision.objects
        .filter(question_set=draft.question_set)
        .aggregate(value=Max('revision_number'))['value']
        or 0
    )
    revision_number = latest_number + 1
    compiled = copy.deepcopy(canonical)
    compiled['schema_id'] = (
        f'question-set:{draft.question_set.public_id}:v{revision_number}'
    )
    compiled['version'] = str(revision_number)
    compiled['effective_settings'] = copy.deepcopy(
        QUESTION_SET_EFFECTIVE_SETTINGS
    )
    revision = QuestionSetRevision.objects.create(
        question_set=draft.question_set,
        revision_number=revision_number,
        source_draft_version=draft.version,
        canonical_body=canonical,
        compiled_protocol=compiled,
        content_hash=content_hash,
        compiler_version=COMPILER_VERSION,
        engine_version=ENGINE_VERSION,
        created_by=actor,
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
        question_set=draft.question_set,
    )
    return revision, True


@transaction.atomic
def issue_preview_capability(*, revision, actor, instructor_session):
    raw_token = secrets.token_urlsafe(32)
    preview = PreviewSession.objects.create(
        revision=revision,
        instructor=actor,
        token_digest=token_digest(raw_token),
        expires_at=timezone.now() + PREVIEW_TTL,
    )
    _record_question_set_event(
        action=InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_STARTED,
        actor=actor,
        instructor_session=instructor_session,
        question_set=revision.question_set,
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
    if not preview.is_valid:
        raise QuestionSetError('preview_expired')
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
def complete_preview(*, raw_token):
    preview = get_preview(raw_token)
    preview = (
        PreviewSession.objects
        .select_for_update()
        .select_related('revision__question_set__course', 'instructor')
        .get(pk=preview.pk)
    )
    protocol = preview.revision.compiled_protocol
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
        )
    return preview


def _new_survey_public_id():
    alphabet = string.ascii_lowercase + string.digits
    for _attempt in range(20):
        public_id = ''.join(secrets.choice(alphabet) for _ in range(10))
        if not FeedbackGPT.objects.filter(public_id=public_id).exists():
            return public_id
    raise QuestionSetError('public_id_generation_failed')


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
):
    idempotency_key = _bounded_text(
        idempotency_key,
        'Idempotency key',
        max_length=100,
    )
    if len(idempotency_key) < 8:
        raise QuestionSetError('invalid_idempotency_key')
    existing = (
        QuestionSetSurvey.objects
        .select_related('survey', 'revision')
        .filter(idempotency_key=idempotency_key)
        .first()
    )
    if existing is not None:
        if existing.revision_id != revision.pk or existing.created_by_id != actor.pk:
            raise QuestionSetError('idempotency_key_conflict')
        return existing, False
    if not revision.preview_sessions.filter(completed_at__isnull=False).exists():
        raise QuestionSetError('preview_required')
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

    survey = FeedbackGPT.objects.create(
        public_id=_new_survey_public_id(),
        name=survey_label,
        survey_label=survey_label,
        instructions=(
            'You are LEAI, a conversational reflection facilitator. Follow the '
            'form-mode directives exactly, ask one question at a time, and keep '
            'your responses concise and supportive.'
        ),
        created_by=actor.display_name,
        course=revision.question_set.course,
        week_number=week_number,
        opens_at=opens_at,
        expires_at=expires_at,
        is_closed=False,
        anonymity_mode='anonymous',
        reporting_structure='',
        mode='form',
        form_schema=None,
    )
    try:
        with transaction.atomic():
            link = QuestionSetSurvey.objects.create(
                survey=survey,
                revision=revision,
                idempotency_key=idempotency_key,
                created_by=actor,
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
        if winner.revision_id != revision.pk or winner.created_by_id != actor.pk:
            raise QuestionSetError('idempotency_key_conflict')
        return winner, False
    record_instructor_event(
        action=InstructorAuditEvent.ACTION_SURVEY_CREATED,
        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        actor=actor,
        session=instructor_session,
        course=revision.question_set.course,
        target_type='survey',
        target_id=survey.pk,
        metadata={'mode': 'form'},
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
    }
