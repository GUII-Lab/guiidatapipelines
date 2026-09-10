import json
import re

from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .instructor_audit import record_instructor_event
from .instructor_auth import (
    authenticate_instructor_request,
    issue_instructor_session,
    normalize_instructor_email,
)
from .models import (
    Course,
    CourseMembership,
    InstitutionMembership,
    InstructorAccount,
    InstructorAuditEvent,
    InstructorSession,
)


COURSE_ID_PATTERN = re.compile(r'^[a-z0-9-]+$')


def _json_object(request):
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _json_response(payload, *, status=200):
    response = JsonResponse(payload, status=status)
    response['Cache-Control'] = 'no-store, private'
    return response


def _authentication_required():
    return _json_response({'error': 'authentication_required'}, status=401)


def _authenticated_account(request):
    account, session = authenticate_instructor_request(request)
    if account is None:
        return None, None, _authentication_required()
    if account.must_change_password:
        return None, None, _json_response(
            {'error': 'password_change_required'},
            status=403,
        )
    return account, session, None


def _serialize_account(account):
    institution_memberships = list(
        account.institution_memberships
        .filter(is_active=True, institution__is_active=True)
        .select_related('institution')
        .order_by('institution__name', 'institution__slug')
    )
    course_memberships = list(
        CourseMembership.objects
        .filter(
            is_active=True,
            institution_membership__in=institution_memberships,
        )
        .select_related('course', 'course__institution')
        .order_by('course__course_id')
    )
    return {
        'id': account.pk,
        'email': account.email,
        'display_name': account.display_name,
        'auth_provider': account.auth_provider,
        'email_verified': account.email_verified_at is not None,
        'must_change_password': account.must_change_password,
        'institutions': [
            {
                'slug': membership.institution.slug,
                'name': membership.institution.name,
                'role': membership.role,
            }
            for membership in institution_memberships
        ],
        'courses': [
            {
                'course_id': membership.course.course_id,
                'course_name': membership.course.course_name,
                'institution_slug': (
                    membership.course.institution.slug
                    if membership.course.institution_id else None
                ),
                'role': membership.role,
                'can_publish': membership.can_publish,
                'can_export': membership.can_export,
            }
            for membership in course_memberships
        ],
    }


@csrf_exempt
def instructor_sessions(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    payload = _json_object(request)
    if payload is None:
        return _json_response({'error': 'invalid_json'}, status=400)

    email = normalize_instructor_email(payload.get('email'))
    password = payload.get('password')
    account = (
        InstructorAccount.objects
        .select_related('user')
        .filter(email=email, is_active=True, user__is_active=True)
        .first()
    )
    if account is None or not isinstance(password, str) or not account.user.check_password(password):
        record_instructor_event(
            action=InstructorAuditEvent.ACTION_LOGIN_DENIED,
            outcome=InstructorAuditEvent.OUTCOME_DENIED,
            actor=account,
        )
        return _json_response({'error': 'invalid_credentials'}, status=401)

    with transaction.atomic():
        raw_token, session = issue_instructor_session(account)
        record_instructor_event(
            action=InstructorAuditEvent.ACTION_LOGIN_SUCCEEDED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            actor=account,
            session=session,
        )
    return _json_response({
        'token': raw_token,
        'expires_at': session.expires_at.isoformat(),
        'must_change_password': account.must_change_password,
    }, status=201)


@csrf_exempt
def instructor_current_session(request):
    if request.method != 'DELETE':
        return HttpResponse(status=405)
    account, session = authenticate_instructor_request(request)
    if account is None:
        return _authentication_required()
    with transaction.atomic():
        session.revoked_at = timezone.now()
        session.save(update_fields=['revoked_at'])
        record_instructor_event(
            action=InstructorAuditEvent.ACTION_LOGOUT,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            actor=account,
            session=session,
        )
    response = HttpResponse(status=204)
    response['Cache-Control'] = 'no-store, private'
    return response


@csrf_exempt
def instructor_me(request):
    if request.method not in {'GET', 'PATCH'}:
        return HttpResponse(status=405)

    if request.method == 'GET':
        account, _session = authenticate_instructor_request(request)
        if account is None:
            return _authentication_required()
        return _json_response(_serialize_account(account))

    account, session, error_response = _authenticated_account(request)
    if error_response is not None:
        return error_response
    payload = _json_object(request)
    if payload is None:
        return _json_response({'error': 'invalid_json'}, status=400)
    if set(payload) != {'display_name'}:
        return _json_response({'error': 'invalid_profile'}, status=400)
    display_name = payload['display_name']
    if not isinstance(display_name, str):
        return _json_response({'error': 'invalid_profile'}, status=400)
    display_name = display_name.strip()
    if not display_name or len(display_name) > 100:
        return _json_response({'error': 'invalid_profile'}, status=400)

    with transaction.atomic():
        account.display_name = display_name
        account.save(update_fields=['display_name', 'updated_at'])
        record_instructor_event(
            action=InstructorAuditEvent.ACTION_PROFILE_UPDATED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            actor=account,
            session=session,
            metadata={'changed_field': 'display_name'},
        )
    return _json_response(_serialize_account(account))


@csrf_exempt
def instructor_password(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    account, current_session = authenticate_instructor_request(request)
    if account is None:
        return _authentication_required()
    payload = _json_object(request)
    if payload is None:
        return _json_response({'error': 'invalid_json'}, status=400)

    current_password = payload.get('current_password')
    new_password = payload.get('new_password')
    if not isinstance(current_password, str) or not account.user.check_password(current_password):
        return _json_response({'error': 'invalid_current_password'}, status=400)
    if not isinstance(new_password, str):
        return _json_response({'error': 'invalid_password', 'details': ['Password is required.']}, status=400)

    try:
        validate_password(new_password, user=account.user)
    except ValidationError as exc:
        return _json_response({
            'error': 'invalid_password',
            'details': list(exc.messages),
        }, status=400)

    now = timezone.now()
    with transaction.atomic():
        account.user.set_password(new_password)
        account.user.save(update_fields=['password'])
        account.must_change_password = False
        account.save(update_fields=['must_change_password', 'updated_at'])
        InstructorSession.objects.filter(
            instructor=account,
            revoked_at__isnull=True,
        ).exclude(pk=current_session.pk).update(revoked_at=now)
        record_instructor_event(
            action=InstructorAuditEvent.ACTION_PASSWORD_CHANGED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            actor=account,
            session=current_session,
        )

    return _json_response({'status': 'password_changed'})


@csrf_exempt
def instructor_courses(request):
    if request.method not in {'GET', 'POST'}:
        return HttpResponse(status=405)
    account, _session, error_response = _authenticated_account(request)
    if error_response is not None:
        return error_response

    if request.method == 'GET':
        memberships = (
            CourseMembership.objects
            .filter(
                is_active=True,
                institution_membership__is_active=True,
                institution_membership__instructor=account,
            )
            .select_related('course', 'course__institution')
            .order_by('course__course_id')
        )
        return _json_response({'courses': [
            {
                'course_id': membership.course.course_id,
                'course_name': membership.course.course_name,
                'instructor_name': membership.course.instructor_name,
                'institution_slug': (
                    membership.course.institution.slug
                    if membership.course.institution_id else None
                ),
                'role': membership.role,
                'can_publish': membership.can_publish,
                'can_export': membership.can_export,
            }
            for membership in memberships
        ]})

    payload = _json_object(request)
    if payload is None:
        return _json_response({'error': 'invalid_json'}, status=400)
    course_id = str(payload.get('course_id') or '').strip().lower()
    course_name = str(payload.get('course_name') or '').strip()
    instructor_name = str(payload.get('instructor_name') or '').strip()
    institution_slug = str(payload.get('institution_slug') or '').strip().lower()
    if (
        not course_id
        or len(course_id) > 50
        or COURSE_ID_PATTERN.fullmatch(course_id) is None
        or not course_name
        or len(course_name) > 200
        or not instructor_name
        or len(instructor_name) > 100
        or not institution_slug
    ):
        return _json_response({'error': 'invalid_course'}, status=400)

    institution_membership = (
        InstitutionMembership.objects
        .select_related('institution')
        .filter(
            instructor=account,
            institution__slug=institution_slug,
            institution__is_active=True,
            is_active=True,
        )
        .first()
    )
    if institution_membership is None:
        record_instructor_event(
            action=InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
            outcome=InstructorAuditEvent.OUTCOME_DENIED,
            actor=account,
            session=_session,
            metadata={'reason_code': 'institution_access_denied'},
        )
        return _json_response({'error': 'institution_access_denied'}, status=403)

    try:
        with transaction.atomic():
            if Course.objects.filter(course_id=course_id).exists():
                return _json_response({'error': 'course_id_taken'}, status=409)
            course = Course.objects.create(
                course_id=course_id,
                course_name=course_name,
                instructor_name=instructor_name,
                password=make_password(None),
                institution=institution_membership.institution,
                legacy_password_login_enabled=False,
            )
            CourseMembership.objects.create(
                course=course,
                institution_membership=institution_membership,
                role=CourseMembership.ROLE_OWNER,
            )
            record_instructor_event(
                action=InstructorAuditEvent.ACTION_COURSE_CREATED,
                outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                actor=account,
                session=_session,
                course=course,
                target_type='course',
                target_id=course.course_id,
                metadata={
                    'institution_slug': institution_membership.institution.slug,
                },
            )
    except IntegrityError:
        return _json_response({'error': 'course_id_taken'}, status=409)

    return _json_response({
        'status': 'success',
        'id': course.pk,
        'course_id': course.course_id,
        'course_name': course.course_name,
        'institution_slug': institution_membership.institution.slug,
        'role': CourseMembership.ROLE_OWNER,
    }, status=201)
