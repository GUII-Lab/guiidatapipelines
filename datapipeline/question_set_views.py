import json

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from .instructor_auth import (
    authenticate_instructor_request,
    authorize_instructor_course,
)
from .instructor_audit import record_instructor_event
from .models import (
    Course,
    InstructorAuditEvent,
    QuestionSetDraft,
    QuestionSetRevision,
)
from .question_sets import (
    QuestionSetError,
    complete_preview,
    create_draft,
    create_survey_from_revision,
    freeze_draft,
    get_preview,
    issue_preview_capability,
    list_templates,
    save_draft,
    save_preview_message,
    serialize_draft,
    serialize_revision,
    serialize_survey_link,
)


def _private_json(payload, status=200):
    response = JsonResponse(payload, status=status)
    response['Cache-Control'] = 'no-store, private'
    return response


def _ready_account(request):
    account, session = authenticate_instructor_request(request)
    if account is None:
        return None, None, _private_json({'error': 'authentication_required'}, 401)
    if account.must_change_password:
        return account, session, _private_json({'error': 'password_change_required'}, 403)
    return account, session, None


def _authorize_course(request, course, *, capability=None):
    account, session, membership, error = authorize_instructor_course(
        request,
        course,
        capability=capability,
    )
    if error is not None and account is not None:
        try:
            code = json.loads(error.content).get('error')
        except (TypeError, ValueError, UnicodeDecodeError):
            code = None
        if code in {'course_access_denied', 'capability_denied'}:
            record_instructor_event(
                action=InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
                outcome=InstructorAuditEvent.OUTCOME_DENIED,
                actor=account,
                session=session,
                course=course,
                target_type='course',
                target_id=course.course_id,
                metadata={'reason_code': code},
            )
    return account, session, membership, error


def _body(request):
    try:
        value = json.loads(request.body or b'{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        raise QuestionSetError('invalid_json')
    if not isinstance(value, dict):
        raise QuestionSetError('invalid_json')
    return value


def _course(course_id):
    if not isinstance(course_id, str) or not course_id.strip():
        raise QuestionSetError('course_id_required')
    try:
        return Course.objects.get(course_id=course_id.strip())
    except Course.DoesNotExist:
        raise QuestionSetError('course_not_found')


def _status_for_error(code):
    return {
        'authentication_required': 401,
        'password_change_required': 403,
        'course_access_denied': 403,
        'capability_denied': 403,
        'course_not_found': 404,
        'template_not_found': 404,
        'draft_not_found': 404,
        'revision_not_found': 404,
        'preview_not_found': 404,
        'preview_expired': 410,
        'stale_draft': 409,
        'preview_required': 409,
        'preview_incomplete': 409,
        'idempotency_key_conflict': 409,
    }.get(code, 400)


def _error(exc):
    payload = {'error': exc.code}
    if exc.message != exc.code:
        payload['message'] = exc.message
    return _private_json(payload, _status_for_error(exc.code))


def _parse_optional_datetime(value, field_name):
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise QuestionSetError('invalid_schedule', f'{field_name} is invalid.')
    parsed = parse_datetime(value)
    if parsed is None:
        raise QuestionSetError('invalid_schedule', f'{field_name} is invalid.')
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _draft_for_id(draft_id):
    try:
        return (
            QuestionSetDraft.objects
            .select_related('question_set__course', 'base_revision')
            .get(public_id=draft_id)
        )
    except (QuestionSetDraft.DoesNotExist, ValueError):
        raise QuestionSetError('draft_not_found')


def _revision_for_id(revision_id):
    try:
        return (
            QuestionSetRevision.objects
            .select_related('question_set__course')
            .get(public_id=revision_id)
        )
    except (QuestionSetRevision.DoesNotExist, ValueError):
        raise QuestionSetError('revision_not_found')


@csrf_exempt
def question_set_templates(request):
    if request.method != 'GET':
        return HttpResponse(status=405)
    _account, _session, error = _ready_account(request)
    if error is not None:
        return error
    return _private_json({'templates': list_templates()})


@csrf_exempt
def question_set_drafts(request):
    try:
        if request.method == 'GET':
            course = _course(request.GET.get('course_id'))
            _account, _session, _membership, error = _authorize_course(
                request, course,
            )
            if error is not None:
                return error
            drafts = (
                QuestionSetDraft.objects
                .select_related('question_set__course', 'base_revision')
                .filter(
                    question_set__course=course,
                    question_set__archived_at__isnull=True,
                )
            )
            return _private_json({
                'drafts': [serialize_draft(draft) for draft in drafts],
            })

        if request.method == 'POST':
            payload = _body(request)
            course = _course(payload.get('course_id'))
            account, session, _membership, error = _authorize_course(
                request, course,
            )
            if error is not None:
                return error
            draft = create_draft(
                course=course,
                actor=account,
                instructor_session=session,
                template_id=str(payload.get('template_id') or ''),
            )
            return _private_json(serialize_draft(draft), 201)
        return HttpResponse(status=405)
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_draft_detail(request, draft_id):
    try:
        draft = _draft_for_id(draft_id)
        account, session, _membership, error = _authorize_course(
            request, draft.question_set.course,
        )
        if error is not None:
            return error
        if request.method == 'GET':
            return _private_json(serialize_draft(draft))
        if request.method == 'PATCH':
            payload = _body(request)
            expected_version = payload.get('expected_version')
            if type(expected_version) is not int:
                raise QuestionSetError('expected_version_required')
            draft = save_draft(
                draft_id=draft.public_id,
                actor=account,
                instructor_session=session,
                expected_version=expected_version,
                body=payload.get('body'),
            )
            return _private_json(serialize_draft(draft))
        return HttpResponse(status=405)
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_draft_freeze(request, draft_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        draft = _draft_for_id(draft_id)
        account, session, _membership, error = _authorize_course(
            request,
            draft.question_set.course,
        )
        if error is not None:
            return error
        payload = _body(request)
        expected_version = payload.get('expected_version')
        if type(expected_version) is not int:
            raise QuestionSetError('expected_version_required')
        revision, created = freeze_draft(
            draft_id=draft.public_id,
            actor=account,
            instructor_session=session,
            expected_version=expected_version,
        )
        return _private_json(
            {'revision': serialize_revision(revision)},
            201 if created else 200,
        )
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_preview_capability(request, revision_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        revision = _revision_for_id(revision_id)
        account, session, _membership, error = _authorize_course(
            request,
            revision.question_set.course,
        )
        if error is not None:
            return error
        raw_token, preview = issue_preview_capability(
            revision=revision,
            actor=account,
            instructor_session=session,
        )
        return _private_json({
            'token': raw_token,
            'expires_at': preview.expires_at.isoformat(),
            'preview_url': f'feedback.html?preview={raw_token}',
        }, 201)
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_preview(request, raw_token):
    if request.method != 'GET':
        return HttpResponse(status=405)
    try:
        preview = get_preview(raw_token)
        revision = preview.revision
        protocol = revision.compiled_protocol
        return _private_json({
            'id': None,
            'public_id': f'preview-{preview.public_id}',
            'name': protocol['title'],
            'survey_label': protocol['title'],
            'mode': 'form',
            'instructions': (
                'You are LEAI, a conversational reflection facilitator. Follow '
                'the form-mode directives exactly, ask one question at a time, '
                'and keep your responses concise and supportive.'
            ),
            'week_number': None,
            'is_active': True,
            'reason': None,
            'anonymity_mode': 'anonymous',
            'is_preview': True,
            'preview_completed': preview.completed_at is not None,
            'question_set_revision_id': str(revision.public_id),
            'form_schema_id': protocol['schema_id'],
            'form_schema': {
                'schema_id': protocol['schema_id'],
                'version': protocol['version'],
                'title': protocol['title'],
                'body': protocol,
            },
            'course_banner': None,
            'bot_display_name': 'LEAI',
            'referral_enabled': False,
            'referral_text': '',
            'identity_tracking_enabled': False,
            'completion_certificate_enabled': False,
            'parsed_document_download_enabled': False,
            'team_snapshot': None,
        })
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_preview_messages(request, raw_token):
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        payload = _body(request)
        message = save_preview_message(
            raw_token=raw_token,
            role=payload.get('role'),
            content=payload.get('content'),
            attribution=payload.get('attribution'),
        )
        return _private_json({
            'id': message.pk,
            'sequence': message.sequence,
        }, 201)
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_preview_complete(request, raw_token):
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        preview = complete_preview(raw_token=raw_token)
        return _private_json({
            'completed': True,
            'completed_at': preview.completed_at.isoformat(),
            'question_set_revision_id': str(preview.revision.public_id),
        })
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_revision_surveys(request, revision_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        revision = _revision_for_id(revision_id)
        account, session, _membership, error = _authorize_course(
            request,
            revision.question_set.course,
            capability='publish',
        )
        if error is not None:
            return error
        payload = _body(request)
        course_id = payload.get('course_id')
        if course_id != revision.question_set.course.course_id:
            raise QuestionSetError('course_mismatch')
        link, created = create_survey_from_revision(
            revision=revision,
            actor=account,
            instructor_session=session,
            idempotency_key=payload.get('idempotency_key'),
            survey_label=payload.get('survey_label'),
            week_number=payload.get('week_number'),
            opens_at=_parse_optional_datetime(payload.get('opens_at'), 'Opening time'),
            expires_at=_parse_optional_datetime(payload.get('expires_at'), 'Closing time'),
        )
        return _private_json(
            serialize_survey_link(link),
            201 if created else 200,
        )
    except QuestionSetError as exc:
        return _error(exc)
