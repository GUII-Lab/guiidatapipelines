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
from .feedback_authoring import (
    run_authoring_request,
    serialize_authoring_conversation,
    serialize_authoring_run,
)
from .models import (
    AuthoringConversation,
    AuthoringRun,
    Course,
    InstructorAuditEvent,
    QuestionSetDraft,
    QuestionSetDraftVersion,
    QuestionSetRevision,
    QuestionSetTemplate,
    QuestionSetTemplateRevision,
    TeamConfiguration,
)
from .question_sets import (
    QuestionSetError,
    complete_preview,
    create_draft,
    create_feedback_draft,
    create_survey_from_revision,
    freeze_draft,
    get_preview,
    issue_preview_capability,
    list_templates,
    restore_feedback_draft,
    save_draft,
    save_feedback_draft,
    save_preview_message,
    save_private_template,
    serialize_preview_settings,
    serialize_preview_status,
    serialize_draft,
    serialize_draft_version,
    serialize_draft_versions,
    serialize_revision,
    serialize_survey_link,
    serialize_template,
    update_preview_settings,
    publish_template_to_community,
    skip_preview,
    withdraw_template_from_community,
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
        'template_access_denied': 403,
        'draft_not_found': 404,
        'draft_version_not_found': 404,
        'authoring_run_not_found': 404,
        'revision_not_found': 404,
        'preview_not_found': 404,
        'preview_expired': 410,
        'preview_preparing': 425,
        'stale_draft': 409,
        'preview_required': 409,
        'preview_incomplete': 409,
        'preview_skip_confirmation_required': 409,
        'preview_token_required': 400,
        'preview_revision_mismatch': 409,
        'preview_owner_mismatch': 403,
        'preview_published': 409,
        'idempotency_key_conflict': 409,
        'active_draft_exists': 409,
        'workflow_not_active': 409,
        'authoring_conflict': 409,
        'authoring_provider_error': 502,
    }.get(code, 400)


def _error(exc):
    payload = {'error': exc.code}
    if exc.message != exc.code:
        payload['message'] = exc.message
    if exc.timing:
        payload.update(exc.timing)
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
            .select_related(
                'question_set__course',
                'question_set__source_template_revision',
                'base_revision',
                'current_checkpoint',
            )
            .get(public_id=draft_id)
        )
    except (QuestionSetDraft.DoesNotExist, ValueError):
        raise QuestionSetError('draft_not_found')


def _require_active_workflow(question_set):
    if question_set.workflow_status != 'active':
        raise QuestionSetError('workflow_not_active')


def _revision_for_id(revision_id):
    try:
        return (
            QuestionSetRevision.objects
            .select_related('question_set__course')
            .get(public_id=revision_id)
        )
    except (QuestionSetRevision.DoesNotExist, ValueError):
        raise QuestionSetError('revision_not_found')


def _template_for_id(template_id):
    try:
        return QuestionSetTemplate.objects.get(public_id=template_id)
    except (QuestionSetTemplate.DoesNotExist, ValueError):
        raise QuestionSetError('template_not_found')


@csrf_exempt
def question_set_templates(request):
    if request.method != 'GET':
        return HttpResponse(status=405)
    account, _session, error = _ready_account(request)
    if error is not None:
        return error
    audience = request.GET.get('audience')
    collection_style = request.GET.get('collection_style')
    try:
        templates = list_templates(
            actor=account if audience is not None or collection_style is not None else None,
            audience=audience,
            collection_style=collection_style,
        )
    except QuestionSetError as exc:
        return _error(exc)
    return _private_json({'templates': templates})


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
                .select_related(
                    'question_set__course',
                    'question_set__source_template_revision',
                    'base_revision',
                    'current_checkpoint',
                )
                .filter(
                    question_set__course=course,
                    question_set__archived_at__isnull=True,
                    question_set__workflow_status='active',
                )
                .order_by('-updated_at', '-id')[:1]
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
            if 'audience' in payload or 'collection_style' in payload or 'source_kind' in payload:
                draft = create_feedback_draft(
                    course=course,
                    actor=account,
                    instructor_session=session,
                    audience=payload.get('audience'),
                    collection_style=payload.get('collection_style'),
                    source_kind=payload.get('source_kind'),
                    source_template_revision_id=payload.get('source_template_revision_id'),
                    confirm_abandon_active=payload.get(
                        'confirm_abandon_active', False,
                    ),
                )
            else:
                draft = create_draft(
                    course=course,
                    actor=account,
                    instructor_session=session,
                    template_id=str(payload.get('template_id') or ''),
                    confirm_abandon_active=payload.get(
                        'confirm_abandon_active', False,
                    ),
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
        _require_active_workflow(draft.question_set)
        if request.method == 'GET':
            return _private_json(serialize_draft(draft))
        if request.method == 'PATCH':
            payload = _body(request)
            expected_version = payload.get('expected_version')
            if type(expected_version) is not int:
                raise QuestionSetError('expected_version_required')
            if 'idempotency_key' in payload:
                draft = save_feedback_draft(
                    draft_id=draft.public_id,
                    actor=account,
                    instructor_session=session,
                    expected_version=expected_version,
                    body=payload.get('body'),
                    idempotency_key=payload.get('idempotency_key'),
                    checkpoint_reason=payload.get('checkpoint_reason'),
                )
            else:
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
def question_set_draft_versions(request, draft_id):
    if request.method != 'GET':
        return HttpResponse(status=405)
    try:
        draft = _draft_for_id(draft_id)
        _account, _session, _membership, error = _authorize_course(
            request,
            draft.question_set.course,
        )
        if error is not None:
            return error
        _require_active_workflow(draft.question_set)
        payload = {'versions': serialize_draft_versions(draft.question_set)}
        selected_id = request.GET.get('version_id')
        if selected_id:
            try:
                selected = QuestionSetDraftVersion.objects.get(
                    public_id=selected_id,
                    question_set=draft.question_set,
                )
            except (QuestionSetDraftVersion.DoesNotExist, ValueError):
                raise QuestionSetError('draft_version_not_found')
            payload['selected'] = serialize_draft_version(selected)
        return _private_json(payload)
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_draft_restore(request, draft_id):
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
        _require_active_workflow(draft.question_set)
        payload = _body(request)
        expected_version = payload.get('expected_version')
        if type(expected_version) is not int:
            raise QuestionSetError('expected_version_required')
        draft = restore_feedback_draft(
            draft_id=draft.public_id,
            actor=account,
            instructor_session=session,
            expected_version=expected_version,
            version_id=payload.get('version_id'),
            idempotency_key=payload.get('idempotency_key'),
        )
        return _private_json(serialize_draft(draft))
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_authoring_conversation(request, draft_id):
    if request.method != 'GET':
        return HttpResponse(status=405)
    try:
        draft = _draft_for_id(draft_id)
        _account, _session, _membership, error = _authorize_course(
            request,
            draft.question_set.course,
        )
        if error is not None:
            return error
        conversation = AuthoringConversation.objects.filter(
            question_set=draft.question_set,
        ).first()
        return _private_json({
            'conversation': (
                serialize_authoring_conversation(conversation)
                if conversation else None
            ),
        })
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_authoring_runs(request, draft_id):
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
        run = run_authoring_request(
            draft_id=draft.public_id,
            actor=account,
            instructor_session=session,
            instruction=payload.get('instruction'),
            expected_version=expected_version,
            idempotency_key=payload.get('idempotency_key'),
        )
        return _private_json({'run': serialize_authoring_run(run)}, 201)
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_authoring_run_detail(request, run_id):
    if request.method != 'GET':
        return HttpResponse(status=405)
    try:
        try:
            run = (
                AuthoringRun.objects
                .select_related(
                    'conversation__question_set__course',
                    'applied_version',
                )
                .get(public_id=run_id)
            )
        except (AuthoringRun.DoesNotExist, ValueError):
            raise QuestionSetError('authoring_run_not_found')
        _account, _session, _membership, error = _authorize_course(
            request,
            run.conversation.question_set.course,
        )
        if error is not None:
            return error
        return _private_json({'run': serialize_authoring_run(run)})
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
        _require_active_workflow(draft.question_set)
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
            'ready_at': preview.ready_at.isoformat(),
            'preview_url': f'feedback.html?preview={raw_token}',
            **serialize_preview_settings(preview),
            **serialize_preview_status(preview),
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
        is_open = protocol.get('schema_version') == 'open-conversation-v1'
        payload = {
            'id': None,
            'public_id': f'preview-{preview.public_id}',
            'name': protocol['title'],
            'survey_label': protocol['title'],
            'mode': 'general' if is_open else 'form',
            'instructions': (
                'You are LEAI, a supportive course-feedback facilitator. Begin '
                f'by asking exactly: "{protocol["opening_prompt"]}" Listen for: '
                f'{protocol["listening_goal"]} Before ending, ask exactly: '
                f'"{protocol["closing_prompt"]}" After the student answers the '
                'closing question, acknowledge them and emit [END] on its own line.'
                if is_open else
                'You are LEAI, a conversational reflection facilitator. Follow '
                'the form-mode directives exactly, ask one question at a time, '
                'and keep your responses concise and supportive.'
            ),
            'week_number': None,
            'is_active': True,
            'reason': None,
            'anonymity_mode': 'anonymous',
            'is_preview': True,
            **serialize_preview_status(preview),
            'question_set_revision_id': str(revision.public_id),
            'course_banner': None,
            'bot_display_name': 'LEAI',
            'referral_enabled': False,
            'referral_text': '',
            'identity_tracking_enabled': False,
            **serialize_preview_settings(preview),
            'team_snapshot': None,
        }
        if not is_open:
            payload.update({
                'form_schema_id': protocol['schema_id'],
                'form_schema': {
                    'schema_id': protocol['schema_id'],
                    'version': protocol['version'],
                    'title': protocol['title'],
                    'body': protocol,
                },
            })
        return _private_json(payload)
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_preview_settings(request, raw_token):
    if request.method != 'PATCH':
        return HttpResponse(status=405)
    try:
        account, session, error = _ready_account(request)
        if error is not None:
            return error
        preview = update_preview_settings(
            raw_token=raw_token,
            actor=account,
            instructor_session=session,
            settings=_body(request),
        )
        return _private_json(serialize_preview_settings(preview))
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
def question_set_preview_skip(request, raw_token):
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        account, session, error = _ready_account(request)
        if error is not None:
            return error
        preview = skip_preview(
            raw_token=raw_token,
            actor=account,
            instructor_session=session,
            acknowledge_warning=_body(request).get('acknowledge_warning'),
        )
        return _private_json(serialize_preview_status(preview))
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
        team_configuration = None
        team_configuration_id = payload.get('team_configuration_id')
        if team_configuration_id not in (None, ''):
            try:
                team_configuration = TeamConfiguration.objects.get(
                    pk=team_configuration_id,
                )
            except (TeamConfiguration.DoesNotExist, TypeError, ValueError):
                raise QuestionSetError('invalid_team_configuration')
        link, created = create_survey_from_revision(
            revision=revision,
            actor=account,
            instructor_session=session,
            idempotency_key=payload.get('idempotency_key'),
            survey_label=payload.get('survey_label'),
            week_number=payload.get('week_number'),
            opens_at=_parse_optional_datetime(payload.get('opens_at'), 'Opening time'),
            expires_at=_parse_optional_datetime(payload.get('expires_at'), 'Closing time'),
            preview_token=payload.get('preview_token'),
            team_configuration=team_configuration,
        )
        return _private_json(
            serialize_survey_link(link),
            201 if created else 200,
        )
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_revision_templates(request, revision_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        revision = _revision_for_id(revision_id)
        account, _session, _membership, error = _authorize_course(
            request,
            revision.question_set.course,
        )
        if error is not None:
            return error
        payload = _body(request)
        template, template_revision = save_private_template(
            revision=revision,
            actor=account,
            name=payload.get('name'),
            description=payload.get('description') or '',
        )
        return _private_json(
            {'template': serialize_template(template, template_revision)},
            201,
        )
    except QuestionSetError as exc:
        return _error(exc)


@csrf_exempt
def question_set_template_community(request, template_id):
    try:
        template = _template_for_id(template_id)
        account, _session, error = _ready_account(request)
        if error is not None:
            return error
        if request.method == 'POST':
            payload = _body(request)
            try:
                revision = QuestionSetTemplateRevision.objects.get(
                    public_id=payload.get('revision_id'),
                    template=template,
                )
            except (QuestionSetTemplateRevision.DoesNotExist, ValueError):
                raise QuestionSetError('template_not_found')
            template = publish_template_to_community(
                template=template,
                revision=revision,
                actor=account,
            )
            return _private_json({'template': serialize_template(template, revision)})
        if request.method == 'DELETE':
            template = withdraw_template_from_community(
                template=template,
                actor=account,
            )
            return _private_json({'template': serialize_template(template)})
        return HttpResponse(status=405)
    except QuestionSetError as exc:
        return _error(exc)
