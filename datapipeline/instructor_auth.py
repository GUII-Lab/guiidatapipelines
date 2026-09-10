import hashlib
import os
import secrets
from datetime import timedelta

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

from .models import CourseMembership, InstructorSession


DEFAULT_SESSION_HOURS = 12


def normalize_instructor_email(value):
    return str(value or '').strip().lower()


def token_digest(raw_token):
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def _session_hours():
    try:
        value = int(os.environ.get('LEAI_INSTRUCTOR_SESSION_HOURS', DEFAULT_SESSION_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_HOURS
    return value if value > 0 else DEFAULT_SESSION_HOURS


@transaction.atomic
def issue_instructor_session(account):
    raw_token = secrets.token_urlsafe(32)
    session = InstructorSession.objects.create(
        instructor=account,
        token_digest=token_digest(raw_token),
        expires_at=timezone.now() + timedelta(hours=_session_hours()),
    )
    return raw_token, session


def authenticate_instructor_request(request):
    authorization = request.headers.get('Authorization', '')
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None, None

    session = (
        InstructorSession.objects
        .select_related('instructor__user')
        .filter(token_digest=token_digest(parts[1]))
        .first()
    )
    if session is None or not session.is_valid:
        return None, None

    now = timezone.now()
    InstructorSession.objects.filter(pk=session.pk).update(last_used_at=now)
    session.last_used_at = now
    return session.instructor, session


def _authorization_error(code, status):
    response = JsonResponse({'error': code}, status=status)
    response['Cache-Control'] = 'no-store, private'
    return response


def membership_allows(membership, capability):
    if membership is None:
        return False
    if capability is None:
        return True
    if capability not in {'publish', 'export'}:
        return False
    if membership.role == CourseMembership.ROLE_OWNER:
        return True
    if capability == 'publish':
        return membership.can_publish
    return membership.can_export


def authorize_instructor_course(request, course, *, capability=None):
    account, session = authenticate_instructor_request(request)
    if account is None:
        return (
            None,
            None,
            None,
            _authorization_error('authentication_required', 401),
        )
    if account.must_change_password:
        return (
            account,
            session,
            None,
            _authorization_error('password_change_required', 403),
        )

    membership = (
        CourseMembership.objects
        .select_related(
            'course',
            'institution_membership',
            'institution_membership__institution',
        )
        .filter(
            course=course,
            is_active=True,
            institution_membership__instructor=account,
            institution_membership__is_active=True,
            institution_membership__institution__is_active=True,
            institution_membership__institution=course.institution,
        )
        .first()
    )
    if membership is None:
        return (
            account,
            session,
            None,
            _authorization_error('course_access_denied', 403),
        )
    if not membership_allows(membership, capability):
        return (
            account,
            session,
            membership,
            _authorization_error('capability_denied', 403),
        )
    return account, session, membership, None
