from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone as datetime_timezone

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .instructor_audit import record_instructor_event
from .models import (
    Course,
    CourseMembership,
    FeedbackGPT,
    FeedbackMessage,
    Institution,
    InstitutionMembership,
    InstructorAccount,
    InstructorAuditEvent,
    LEAIChatMessage,
    LEAIChatSession,
    PreviewMessage,
    PreviewSession,
    QuestionSet,
    QuestionSetDraft,
    QuestionSetRevision,
    QuestionSetSurvey,
    QuestionSetValidationRun,
    ResponseSession,
)
from .question_sets import (
    COMPILER_VERSION,
    ENGINE_VERSION,
    QUESTION_SET_EFFECTIVE_SETTINGS,
    complete_preview,
    create_draft,
    create_survey_from_revision,
    freeze_draft,
    get_template,
    issue_preview_capability,
    save_preview_message,
    validate_body,
)
from .response_sessions import persist_feedback_messages


QA_SEED_VERSION = '2026-09-12.1'
QA_INSTITUTION_SLUG = 'qa-ucsc'
QA_INSTRUCTOR_EMAILS = (
    'primary-instructor@qa.invalid',
    'reviewer-instructor@qa.invalid',
)
QA_INSTRUCTOR_USERNAMES = (
    'leai-qa-primary-instructor',
    'leai-qa-reviewer-instructor',
)
QA_COURSE_IDS = (
    'qa-upcoming-course',
    'qa-active-course',
    'qa-completed-course',
)
QA_SURVEY_PUBLIC_ID = 'qa-active-w4'
QA_SURVEY_IDEMPOTENCY_KEY = 'qa-published-reflection-v1'
QA_CLIENT_SESSION_ID = 'qa-anonymous-completed'


def _stable_uuid(label: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f'leai-qa-seed:{QA_SEED_VERSION}:{label}')


QA_QUESTION_SET_PUBLIC_ID = _stable_uuid('question-set')
QA_DRAFT_PUBLIC_ID = _stable_uuid('question-set-draft')
QA_REVISION_PUBLIC_ID = _stable_uuid('question-set-revision')
QA_PREVIEW_PUBLIC_ID = _stable_uuid('preview-session')
QA_RESPONSE_PUBLIC_ID = _stable_uuid('response-session')
QA_ANALYSIS_SESSION_ID = _stable_uuid('analysis-session')
QA_AUDIT_EVENT_IDS = tuple(
    _stable_uuid(f'audit:{label}')
    for label in (
        'course-upcoming-created',
        'course-active-created',
        'course-completed-created',
        'question-set-draft-created',
        'question-set-revision-frozen',
        'question-set-preview-started',
        'question-set-preview-completed',
        'survey-created',
        'analysis-session-created',
    )
)

EXPECTED_COUNTS = {
    'institutions': 1,
    'instructor_accounts': 2,
    'auth_users': 2,
    'institution_memberships': 2,
    'courses': 3,
    'course_memberships': 4,
    'question_sets': 1,
    'question_set_drafts': 1,
    'question_set_revisions': 1,
    'question_set_validation_runs': 2,
    'preview_sessions': 1,
    'preview_messages': 11,
    'feedback_gpts': 1,
    'question_set_surveys': 1,
    'response_sessions': 1,
    'feedback_messages': 11,
    'analysis_sessions': 1,
    'analysis_messages': 2,
    'instructor_audit_events': 9,
}

_COURSE_SCENARIOS = (
    ('qa-upcoming-course', 'QA Upcoming Course'),
    ('qa-active-course', 'QA Active Course'),
    ('qa-completed-course', 'QA Completed Course'),
)
_FIXED_PREVIEW_EXPIRY = datetime(2099, 1, 1, tzinfo=datetime_timezone.utc)


class QASeedError(RuntimeError):
    pass


def _require_qa_environment(*, reset: bool, confirm: str | None) -> None:
    if getattr(settings, 'LEAI_ENV', None) != 'qa':
        raise QASeedError('LEAI QA seed is available only when LEAI_ENV is qa.')
    if reset and confirm != 'qa':
        raise QASeedError('QA reset requires exact confirmation: qa.')


def _normalized_passwords(instructor_passwords) -> dict[str, str]:
    if instructor_passwords is None:
        return {}
    if not isinstance(instructor_passwords, dict):
        raise QASeedError('Instructor passwords must be supplied by QA email.')
    unknown = set(instructor_passwords) - set(QA_INSTRUCTOR_EMAILS)
    if unknown:
        raise QASeedError('Instructor passwords included an unknown QA identity.')
    normalized = {}
    for email, value in instructor_passwords.items():
        if not isinstance(value, str) or not value:
            raise QASeedError('Instructor passwords cannot be blank.')
        normalized[email] = value
    return normalized


def _reset_owned_children() -> None:
    InstructorAuditEvent.objects.filter(event_id__in=QA_AUDIT_EVENT_IDS).delete()
    LEAIChatSession.objects.filter(pk=QA_ANALYSIS_SESSION_ID).delete()
    ResponseSession.objects.filter(public_id=QA_RESPONSE_PUBLIC_ID).delete()
    PreviewSession.objects.filter(public_id=QA_PREVIEW_PUBLIC_ID).delete()
    QuestionSetValidationRun.objects.filter(
        Q(draft__public_id=QA_DRAFT_PUBLIC_ID)
        | Q(revision__public_id=QA_REVISION_PUBLIC_ID)
    ).delete()


def _upsert_instructor(*, email, username, display_name, password):
    user_model = get_user_model()
    account = InstructorAccount.objects.filter(email=email).select_related('user').first()
    username_user = user_model.objects.filter(username=username).first()
    if account is not None and username_user is not None and account.user_id != username_user.pk:
        raise QASeedError(f'QA username is already attached to another account: {username}')

    if account is not None:
        user = account.user
    elif username_user is not None:
        if InstructorAccount.objects.filter(user=username_user).exists():
            raise QASeedError(f'QA username is already attached to another instructor: {username}')
        user = username_user
    else:
        if password is None:
            raise QASeedError(f'Runtime credential required for missing QA instructor: {email}')
        user = user_model(username=username)

    if password is not None:
        user.set_password(password)
    elif account is None or not user.password:
        raise QASeedError(f'Runtime credential required for missing QA instructor: {email}')
    user.username = username
    user.email = email
    user.is_active = True
    user.save()

    if account is None:
        account = InstructorAccount.objects.create(
            user=user,
            email=email,
            display_name=display_name,
            auth_provider=InstructorAccount.AUTH_MANUAL,
            must_change_password=False,
            is_active=True,
        )
    else:
        account.user = user
        account.display_name = display_name
        account.auth_provider = InstructorAccount.AUTH_MANUAL
        account.external_subject = None
        account.email_verified_at = None
        account.must_change_password = False
        account.is_active = True
        account.save()
    return account


def _upsert_base_graph(passwords: dict[str, str]):
    institution, _created = Institution.objects.update_or_create(
        slug=QA_INSTITUTION_SLUG,
        defaults={
            'name': 'QA University of California, Santa Cruz',
            'is_active': True,
        },
    )
    primary = _upsert_instructor(
        email=QA_INSTRUCTOR_EMAILS[0],
        username=QA_INSTRUCTOR_USERNAMES[0],
        display_name='QA Primary Instructor',
        password=passwords.get(QA_INSTRUCTOR_EMAILS[0]),
    )
    reviewer = _upsert_instructor(
        email=QA_INSTRUCTOR_EMAILS[1],
        username=QA_INSTRUCTOR_USERNAMES[1],
        display_name='QA Reviewer Instructor',
        password=passwords.get(QA_INSTRUCTOR_EMAILS[1]),
    )
    primary_institution, _created = InstitutionMembership.objects.update_or_create(
        institution=institution,
        instructor=primary,
        defaults={
            'role': InstitutionMembership.ROLE_ADMIN,
            'is_active': True,
        },
    )
    reviewer_institution, _created = InstitutionMembership.objects.update_or_create(
        institution=institution,
        instructor=reviewer,
        defaults={
            'role': InstitutionMembership.ROLE_MEMBER,
            'is_active': True,
        },
    )

    courses = {}
    for course_id, course_name in _COURSE_SCENARIOS:
        course = Course.objects.filter(course_id=course_id).first()
        if course is None:
            course = Course.objects.create(
                course_id=course_id,
                course_name=course_name,
                instructor_name=primary.display_name,
                password=make_password(None),
                institution=institution,
                legacy_password_login_enabled=False,
            )
        else:
            course.course_name = course_name
            course.instructor_name = primary.display_name
            course.institution = institution
            course.legacy_password_login_enabled = False
            course.save()
        CourseMembership.objects.update_or_create(
            course=course,
            institution_membership=primary_institution,
            defaults={
                'role': CourseMembership.ROLE_OWNER,
                'can_publish': True,
                'can_export': True,
                'is_active': True,
            },
        )
        courses[course_id] = course

    CourseMembership.objects.update_or_create(
        course=courses['qa-active-course'],
        institution_membership=reviewer_institution,
        defaults={
            'role': CourseMembership.ROLE_INSTRUCTOR,
            'can_publish': True,
            'can_export': True,
            'is_active': True,
        },
    )
    return institution, primary, reviewer, courses


def _expected_revision_values(question_set_public_id):
    body, validation = validate_body(get_template('weekly-reflection')['body'])
    compiled = copy.deepcopy(body)
    compiled['schema_id'] = f'question-set:{question_set_public_id}:v1'
    compiled['version'] = '1'
    compiled['effective_settings'] = copy.deepcopy(QUESTION_SET_EFFECTIVE_SETTINGS)
    revision_identity = {
        'canonical_body': body,
        'compiler_version': COMPILER_VERSION,
        'engine_version': ENGINE_VERSION,
    }
    content_hash = hashlib.sha256(
        json.dumps(
            revision_identity,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
        ).encode('utf-8')
    ).hexdigest()
    return body, validation, compiled, content_hash


def _ensure_question_set_graph(*, course, primary, reset):
    body, validation, expected_compiled, expected_hash = _expected_revision_values(
        QA_QUESTION_SET_PUBLIC_ID
    )
    question_set = QuestionSet.objects.filter(
        public_id=QA_QUESTION_SET_PUBLIC_ID,
    ).first()
    if question_set is None:
        draft = create_draft(
            course=course,
            actor=primary,
            instructor_session=None,
            template_id='weekly-reflection',
            question_set_public_id=QA_QUESTION_SET_PUBLIC_ID,
            draft_public_id=QA_DRAFT_PUBLIC_ID,
            audit_event_id=QA_AUDIT_EVENT_IDS[3],
        )
        question_set = draft.question_set
    else:
        try:
            draft = QuestionSetDraft.objects.get(public_id=QA_DRAFT_PUBLIC_ID)
        except QuestionSetDraft.DoesNotExist as exc:
            raise QASeedError('QA draft is missing; run a confirmed reset.') from exc
        if draft.question_set_id != question_set.pk:
            raise QASeedError('QA draft is attached to the wrong question set.')

    if (
        question_set.course_id != course.pk
        or question_set.owner_id != primary.pk
        or question_set.template_id != 'weekly-reflection'
    ):
        raise QASeedError('QA question set identity conflicts with existing data.')
    if reset:
        question_set.title = body['title']
        question_set.audience = QuestionSet.AUDIENCE_INDIVIDUAL
        question_set.archived_at = None
        question_set.save(update_fields=['title', 'audience', 'archived_at', 'updated_at'])

    revision = QuestionSetRevision.objects.filter(
        public_id=QA_REVISION_PUBLIC_ID,
    ).first()
    if revision is None:
        if QuestionSetRevision.objects.filter(
            question_set=question_set,
            revision_number=1,
        ).exists():
            raise QASeedError('QA revision number conflicts with existing data.')
        if reset:
            draft.body = copy.deepcopy(body)
            draft.version = 1
            draft.base_revision = None
            draft.updated_by = primary
            draft.save(update_fields=[
                'body',
                'version',
                'base_revision',
                'updated_by',
                'updated_at',
            ])
        revision, created = freeze_draft(
            draft_id=draft.public_id,
            actor=primary,
            instructor_session=None,
            expected_version=draft.version,
            revision_public_id=QA_REVISION_PUBLIC_ID,
            audit_event_id=QA_AUDIT_EVENT_IDS[4],
        )
        if not created:
            raise QASeedError('QA revision was not created from the current compiler.')

    if (
        revision.question_set_id != question_set.pk
        or revision.revision_number != 1
        or revision.source_draft_version != 1
        or revision.canonical_body != body
        or revision.compiled_protocol != expected_compiled
        or revision.content_hash != expected_hash
        or revision.compiler_version != COMPILER_VERSION
        or revision.engine_version != ENGINE_VERSION
        or revision.created_by_id != primary.pk
    ):
        raise QASeedError('QA immutable revision differs from the current seed contract.')
    if reset:
        draft.body = copy.deepcopy(body)
        draft.version = 1
        draft.base_revision = revision
        draft.updated_by = primary
        draft.save(update_fields=[
            'body',
            'version',
            'base_revision',
            'updated_by',
            'updated_at',
        ])
    elif draft.body != body or draft.version != 1:
        raise QASeedError('QA draft differs from the current seed contract; reset it first.')
    if draft.base_revision_id != revision.pk:
        draft.base_revision = revision
        draft.save(update_fields=['base_revision', 'updated_at'])
    if not QuestionSetValidationRun.objects.filter(draft=draft).exists():
        QuestionSetValidationRun.objects.create(
            draft=draft,
            is_valid=True,
            result={'errors': [], 'question_count': len(body['sections'])},
        )
    if not QuestionSetValidationRun.objects.filter(revision=revision).exists():
        QuestionSetValidationRun.objects.create(
            revision=revision,
            is_valid=True,
            result=validation,
        )
    return question_set, draft, revision


def _preview_payloads(revision):
    schema_id = revision.compiled_protocol['schema_id']
    schema_version = revision.compiled_protocol['version']
    payloads = []
    for section in revision.compiled_protocol['sections']:
        for field in section['fields']:
            attribution = {
                'form_schema_id': schema_id,
                'form_schema_version': schema_version,
                'form_section_id': section['id'],
                'form_field_id': field['id'],
                'form_field_label': field['label'],
                'form_response_phase': 'primary',
            }
            payloads.extend((
                ('assistant', field['label'], attribution),
                ('user', f'Synthetic QA response for {field["id"]}.', attribution),
            ))
    payloads.extend((
        (
            'assistant',
            revision.compiled_protocol['closing']['feedback_prompt'],
            {},
        ),
        ('user', 'Synthetic QA closing response.', {}),
        ('assistant', 'Synthetic QA reflection complete.', {}),
    ))
    return payloads


def _ensure_preview(*, revision, primary):
    preview = PreviewSession.objects.filter(public_id=QA_PREVIEW_PUBLIC_ID).first()
    if preview is None:
        raw_token, preview = issue_preview_capability(
            revision=revision,
            actor=primary,
            instructor_session=None,
            preview_public_id=QA_PREVIEW_PUBLIC_ID,
            audit_event_id=QA_AUDIT_EVENT_IDS[5],
        )
        PreviewSession.objects.filter(pk=preview.pk).update(
            expires_at=_FIXED_PREVIEW_EXPIRY,
        )
        preview.refresh_from_db()
        for role, content, attribution in _preview_payloads(revision):
            save_preview_message(
                raw_token=raw_token,
                role=role,
                content=content,
                attribution=copy.deepcopy(attribution),
            )
        preview = complete_preview(
            raw_token=raw_token,
            audit_event_id=QA_AUDIT_EVENT_IDS[6],
        )
    if preview.revision_id != revision.pk or preview.completed_at is None:
        raise QASeedError('QA preview does not prove the seeded revision.')
    return preview


def _survey_values(*, revision, primary):
    return {
        'name': 'QA Active Course Week 4 Reflection',
        'survey_label': 'QA Active Course Week 4 Reflection',
        'instructions': (
            'You are LEAI, a conversational reflection facilitator. Follow the '
            'form-mode directives exactly, ask one question at a time, and keep '
            'your responses concise and supportive.'
        ),
        'created_by': primary.display_name,
        'course': revision.question_set.course,
        'week_number': 4,
        'opens_at': None,
        'expires_at': None,
        'is_closed': False,
        'anonymity_mode': 'anonymous',
        'reporting_structure': '',
        'canvas_integration': False,
        'mode': 'form',
        'form_schema': None,
    }


def _ensure_survey(*, revision, primary, reset):
    expected_survey = _survey_values(revision=revision, primary=primary)
    survey = FeedbackGPT.objects.filter(public_id=QA_SURVEY_PUBLIC_ID).first()
    if survey is None:
        if QuestionSetSurvey.objects.filter(
            idempotency_key=QA_SURVEY_IDEMPOTENCY_KEY,
        ).exists():
            raise QASeedError('QA survey idempotency key conflicts with existing data.')
        link, created = create_survey_from_revision(
            revision=revision,
            actor=primary,
            instructor_session=None,
            idempotency_key=QA_SURVEY_IDEMPOTENCY_KEY,
            survey_label='QA Active Course Week 4 Reflection',
            week_number=4,
            opens_at=None,
            expires_at=None,
            survey_public_id=QA_SURVEY_PUBLIC_ID,
            audit_event_id=QA_AUDIT_EVENT_IDS[7],
        )
        if not created:
            raise QASeedError('QA published survey was not created.')
        survey = link.survey
    else:
        link = QuestionSetSurvey.objects.filter(
            idempotency_key=QA_SURVEY_IDEMPOTENCY_KEY,
        ).first()
        survey_link = QuestionSetSurvey.objects.filter(survey=survey).first()
        if link is not None and link.survey_id != survey.pk:
            raise QASeedError('QA survey idempotency key conflicts with existing data.')
        if survey_link is not None and survey_link.pk != getattr(link, 'pk', None):
            raise QASeedError('QA survey is linked through non-seed data.')
        if link is None:
            link = QuestionSetSurvey.objects.create(
                survey=survey,
                revision=revision,
                idempotency_key=QA_SURVEY_IDEMPOTENCY_KEY,
                created_by=primary,
            )
    if reset:
        for field_name, value in expected_survey.items():
            setattr(survey, field_name, value)
        survey.save(update_fields=[*expected_survey, 'updated_at'])
        link.revision = revision
        link.created_by = primary
        link.save(update_fields=['revision', 'created_by'])
    if (
        link.survey_id != survey.pk
        or link.revision_id != revision.pk
        or link.created_by_id != primary.pk
        or any(
            getattr(survey, field_name) != value
            for field_name, value in expected_survey.items()
        )
    ):
        raise QASeedError('QA published survey differs from the seed contract.')
    return survey, link


def _student_payloads(*, revision, survey):
    payloads = []
    for role, content, attribution in _preview_payloads(revision):
        payload = {
            'session_id': QA_CLIENT_SESSION_ID,
            'student_id': 'qa-anonymous',
            'sent_by': 'user-message' if role == 'user' else 'ai-message',
            'content': content,
            'gpt_used': survey.name,
            'gpt_id': survey.pk,
            'research_consent': False,
        }
        payload.update(attribution)
        payloads.append(payload)
    return payloads


def _ensure_response(*, revision, survey):
    response = ResponseSession.objects.filter(public_id=QA_RESPONSE_PUBLIC_ID).first()
    if response is None:
        messages = persist_feedback_messages(
            _student_payloads(revision=revision, survey=survey)
        )
        response = messages[0].response_session
        ResponseSession.objects.filter(pk=response.pk).update(
            public_id=QA_RESPONSE_PUBLIC_ID,
            completed_at=timezone.now(),
        )
        response.refresh_from_db()
    if (
        response.course_id != survey.course_id
        or response.survey_id != survey.pk
        or response.client_session_id != QA_CLIENT_SESSION_ID
        or response.source != ResponseSession.SOURCE_STUDENT
        or response.completed_at is None
    ):
        raise QASeedError('QA response session differs from the seed contract.')
    return response


def _ensure_analysis(*, course, survey, response):
    analysis, created = LEAIChatSession.objects.update_or_create(
        pk=QA_ANALYSIS_SESSION_ID,
        defaults={
            'course': course,
            'title': 'QA Active Course Analysis Example',
            'scope_kind': 'custom',
            'scope_week_number': None,
            'scope_survey_ids': [survey.pk],
            'scope_session_ids': [response.client_session_id],
            'system_prompt_override': None,
        },
    )
    if created:
        LEAIChatMessage.objects.create(
            session=analysis,
            role='user',
            text='Summarize the synthetic QA reflection.',
            cited=[],
            status=LEAIChatMessage.STATUS_READY,
        )
        LEAIChatMessage.objects.create(
            session=analysis,
            role='assistant',
            text='The synthetic response demonstrates a completed analysis workflow.',
            cited=[],
            status=LEAIChatMessage.STATUS_READY,
        )
    return analysis


def _ensure_audit_event(*, event_id, action, actor, course, target_type, target_id, metadata):
    event = InstructorAuditEvent.objects.filter(event_id=event_id).first()
    if event is None:
        event = record_instructor_event(
            event_id=event_id,
            action=action,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            actor=actor,
            session=None,
            course=course,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
        )
    expected_target_id = str(target_id)
    if (
        event.action != action
        or event.outcome != InstructorAuditEvent.OUTCOME_SUCCESS
        or event.actor_id != actor.pk
        or event.session_id is not None
        or event.course_id != course.pk
        or event.course_id_snapshot != course.course_id
        or event.target_type != target_type
        or event.target_id != expected_target_id
        or event.metadata != metadata
    ):
        raise QASeedError('QA audit event identifier conflicts with existing data.')


def _ensure_audit_graph(*, institution, primary, courses, question_set, survey, analysis):
    specs = [
        (
            QA_AUDIT_EVENT_IDS[index],
            InstructorAuditEvent.ACTION_COURSE_CREATED,
            course,
            'course',
            course.course_id,
            {'institution_slug': institution.slug},
        )
        for index, course in enumerate(courses.values())
    ]
    specs.extend((
        (
            QA_AUDIT_EVENT_IDS[3],
            InstructorAuditEvent.ACTION_QUESTION_SET_DRAFT_CREATED,
            courses['qa-active-course'],
            'question_set',
            question_set.public_id,
            {},
        ),
        (
            QA_AUDIT_EVENT_IDS[4],
            InstructorAuditEvent.ACTION_QUESTION_SET_REVISION_FROZEN,
            courses['qa-active-course'],
            'question_set',
            question_set.public_id,
            {},
        ),
        (
            QA_AUDIT_EVENT_IDS[5],
            InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_STARTED,
            courses['qa-active-course'],
            'question_set',
            question_set.public_id,
            {},
        ),
        (
            QA_AUDIT_EVENT_IDS[6],
            InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_COMPLETED,
            courses['qa-active-course'],
            'question_set',
            question_set.public_id,
            {},
        ),
        (
            QA_AUDIT_EVENT_IDS[7],
            InstructorAuditEvent.ACTION_SURVEY_CREATED,
            courses['qa-active-course'],
            'survey',
            survey.pk,
            {'mode': 'form'},
        ),
        (
            QA_AUDIT_EVENT_IDS[8],
            InstructorAuditEvent.ACTION_ANALYSIS_SESSION_CREATED,
            courses['qa-active-course'],
            'analysis_session',
            analysis.pk,
            {},
        ),
    ))
    for event_id, action, course, target_type, target_id, metadata in specs:
        _ensure_audit_event(
            event_id=event_id,
            action=action,
            actor=primary,
            course=course,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
        )


def _persisted_counts() -> dict[str, int]:
    return {
        'institutions': Institution.objects.filter(slug=QA_INSTITUTION_SLUG).count(),
        'instructor_accounts': InstructorAccount.objects.filter(
            email__in=QA_INSTRUCTOR_EMAILS,
        ).count(),
        'auth_users': get_user_model().objects.filter(
            username__in=QA_INSTRUCTOR_USERNAMES,
        ).count(),
        'institution_memberships': InstitutionMembership.objects.filter(
            institution__slug=QA_INSTITUTION_SLUG,
            instructor__email__in=QA_INSTRUCTOR_EMAILS,
        ).count(),
        'courses': Course.objects.filter(course_id__in=QA_COURSE_IDS).count(),
        'course_memberships': CourseMembership.objects.filter(
            course__course_id__in=QA_COURSE_IDS,
            institution_membership__instructor__email__in=QA_INSTRUCTOR_EMAILS,
        ).count(),
        'question_sets': QuestionSet.objects.filter(
            public_id=QA_QUESTION_SET_PUBLIC_ID,
        ).count(),
        'question_set_drafts': QuestionSetDraft.objects.filter(
            public_id=QA_DRAFT_PUBLIC_ID,
        ).count(),
        'question_set_revisions': QuestionSetRevision.objects.filter(
            public_id=QA_REVISION_PUBLIC_ID,
        ).count(),
        'question_set_validation_runs': QuestionSetValidationRun.objects.filter(
            Q(draft__public_id=QA_DRAFT_PUBLIC_ID)
            | Q(revision__public_id=QA_REVISION_PUBLIC_ID)
        ).count(),
        'preview_sessions': PreviewSession.objects.filter(
            public_id=QA_PREVIEW_PUBLIC_ID,
        ).count(),
        'preview_messages': PreviewMessage.objects.filter(
            preview_session__public_id=QA_PREVIEW_PUBLIC_ID,
        ).count(),
        'feedback_gpts': FeedbackGPT.objects.filter(
            public_id=QA_SURVEY_PUBLIC_ID,
        ).count(),
        'question_set_surveys': QuestionSetSurvey.objects.filter(
            idempotency_key=QA_SURVEY_IDEMPOTENCY_KEY,
        ).count(),
        'response_sessions': ResponseSession.objects.filter(
            public_id=QA_RESPONSE_PUBLIC_ID,
        ).count(),
        'feedback_messages': FeedbackMessage.objects.filter(
            response_session__public_id=QA_RESPONSE_PUBLIC_ID,
        ).count(),
        'analysis_sessions': LEAIChatSession.objects.filter(
            pk=QA_ANALYSIS_SESSION_ID,
        ).count(),
        'analysis_messages': LEAIChatMessage.objects.filter(
            session_id=QA_ANALYSIS_SESSION_ID,
        ).count(),
        'instructor_audit_events': InstructorAuditEvent.objects.filter(
            event_id__in=QA_AUDIT_EVENT_IDS,
        ).count(),
    }


def seed_qa_data(
    reset: bool = False,
    *,
    confirm: str | None = None,
    instructor_passwords: dict[str, str] | None = None,
) -> dict[str, int | str]:
    _require_qa_environment(reset=reset, confirm=confirm)
    passwords = _normalized_passwords(instructor_passwords)
    if reset:
        missing = set(QA_INSTRUCTOR_EMAILS) - set(passwords)
        if missing:
            raise QASeedError('Confirmed QA reset requires both runtime credentials.')

    with transaction.atomic():
        if reset:
            _reset_owned_children()
        institution, primary, _reviewer, courses = _upsert_base_graph(passwords)
        question_set, _draft, revision = _ensure_question_set_graph(
            course=courses['qa-active-course'],
            primary=primary,
            reset=reset,
        )
        _ensure_preview(revision=revision, primary=primary)
        survey, _link = _ensure_survey(
            revision=revision,
            primary=primary,
            reset=reset,
        )
        response = _ensure_response(revision=revision, survey=survey)
        analysis = _ensure_analysis(
            course=courses['qa-active-course'],
            survey=survey,
            response=response,
        )
        _ensure_audit_graph(
            institution=institution,
            primary=primary,
            courses=courses,
            question_set=question_set,
            survey=survey,
            analysis=analysis,
        )
        counts = _persisted_counts()
        if counts != EXPECTED_COUNTS:
            raise QASeedError(
                'QA seed persisted-count mismatch: '
                + json.dumps(counts, sort_keys=True)
            )
        return {'seed_version': QA_SEED_VERSION, **counts}
