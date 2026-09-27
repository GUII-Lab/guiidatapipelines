"""Same-origin browser authentication with Django sessions and CSRF protection."""

import json
import uuid

from django.contrib.auth import logout
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed
from django.utils import timezone
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect

from leai.models import AuditEvent, InstitutionMembership, InstructorAccount, InstructorSession
from leai.services.instructor_sessions import (
    create_instructor_session,
    bind_browser_session,
    resolve_instructor_session,
)
from leai.services.login_throttle import LoginRateLimited, consume_login_attempt

from .environment import no_store_json


_UNKNOWN_ACCOUNT_HASH = make_password("unusable-unknown-account-padding")


def _method_not_allowed(methods):
    response = HttpResponseNotAllowed(methods)
    response["Cache-Control"] = "no-store"
    return response


def _invalid_credentials():
    return no_store_json({"error": "invalid_credentials"}, status=401)


def _authentication_required():
    return no_store_json({"error": "authentication_required"}, status=401)


def _strict_login_body(request):
    if request.content_type != "application/json" or len(request.body) > 8192:
        return None
    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"email", "password"}:
        return None
    email, password = payload["email"], payload["password"]
    if not isinstance(email, str) or not isinstance(password, str):
        return None
    email = email.strip().lower()
    if not email or not password or len(email) > 254 or len(password) > 1024:
        return None
    try:
        validate_email(email)
    except ValidationError:
        return None
    return email, password


def _strict_password_change_body(request):
    if request.content_type != "application/json" or len(request.body) > 8192:
        return None
    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"current_password", "new_password"}:
        return None
    current, new = payload["current_password"], payload["new_password"]
    if not isinstance(current, str) or not isinstance(new, str):
        return None
    if not current or not new or len(current) > 1024 or len(new) > 1024:
        return None
    return current, new


def _strict_profile_body(request):
    if request.content_type != "application/json" or len(request.body) > 8192:
        return None
    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"display_name"}:
        return None
    name = payload["display_name"]
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name if 1 <= len(name) <= 100 else None


def _account_payload(account):
    memberships = InstitutionMembership.objects.filter(
        account=account, is_active=True,
    ).select_related("institution").order_by("institution__name", "institution__slug")
    return {
        "id": str(account.public_id),
        "email": account.email,
        "display_name": account.display_name,
        "must_change_password": False,
        "platform_role": account.platform_role,
        "institutions": [
            {
                "slug": member.institution.slug,
                "name": member.institution.name,
                "can_create_courses": member.role == "instructor",
            }
            for member in memberships
        ],
    }


def instructor_csrf_view(request):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    return no_store_json({"csrf_token": get_token(request)})


def csrf_failure(request, reason=""):
    return no_store_json({"error": "csrf_failed"}, status=403)


@csrf_protect
def instructor_sessions_view(request):
    if request.method == "POST":
        credentials = _strict_login_body(request)
        if credentials is None:
            return no_store_json({"error": "invalid_request"}, status=400)
        email, password = credentials
        try:
            consume_login_attempt(email, request.META.get("REMOTE_ADDR", "unknown"))
        except LoginRateLimited as error:
            response = no_store_json({"error": "rate_limited"}, status=429)
            response["Retry-After"] = str(error.retry_after)
            return response
        with transaction.atomic():
            try:
                account = InstructorAccount.objects.select_for_update().select_related("user").get(
                    email__iexact=email
                )
            except (InstructorAccount.DoesNotExist, InstructorAccount.MultipleObjectsReturned):
                check_password(password, _UNKNOWN_ACCOUNT_HASH)
                return _invalid_credentials()
            if (
                not account.user.check_password(password)
                or not account.is_active
                or not account.user.is_active
            ):
                return _invalid_credentials()
            _, expires_at, session = create_instructor_session(account)
            AuditEvent.objects.create(
                actor_account=account,
                actor_kind=(
                    "platform_admin"
                    if account.platform_role == "platform_admin"
                    else "instructor"
                ),
                action="auth.login",
                outcome="allowed",
                target_type="instructor_session",
                target_id=str(session.pk),
                request_id=uuid.uuid4().hex,
                bounded_metadata={},
            )
        bind_browser_session(request, session)
        return no_store_json(
            {
                "expires_at": expires_at.isoformat(),
                "must_change_password": False,
            },
            status=201,
        )
    if request.method == "DELETE":
        session = resolve_instructor_session(request, allow_revoked=True)
        if session is None:
            logout(request)
            response = HttpResponse(status=204)
            response["Cache-Control"] = "no-store"
            return response
        with transaction.atomic():
            locked = InstructorSession.objects.select_for_update().get(pk=session.pk)
            if locked.revoked_at is None:
                locked.revoked_at = timezone.now()
                locked.save(update_fields=["revoked_at"])
                AuditEvent.objects.create(
                    actor_account=locked.account,
                    actor_kind=(
                        "platform_admin"
                        if locked.account.platform_role == "platform_admin"
                        else "instructor"
                    ),
                    action="auth.logout",
                    outcome="allowed",
                    target_type="instructor_session",
                    target_id=str(locked.pk),
                    request_id=uuid.uuid4().hex,
                    bounded_metadata={},
                )
        logout(request)
        response = HttpResponse(status=204)
        response["Cache-Control"] = "no-store"
        return response
    return _method_not_allowed(["POST", "DELETE"])


@csrf_protect
def instructor_me_view(request):
    if request.method not in {"GET", "PATCH"}:
        return _method_not_allowed(["GET", "PATCH"])
    session = resolve_instructor_session(request)
    if session is None:
        return _authentication_required()
    if request.method == "GET":
        return no_store_json(_account_payload(session.account))
    name = _strict_profile_body(request)
    if name is None:
        return no_store_json({"error": "invalid_request"}, status=400)
    with transaction.atomic():
        account = InstructorAccount.objects.select_for_update().get(pk=session.account_id)
        if account.display_name != name:
            account.display_name = name
            account.save(update_fields=["display_name"])
            AuditEvent.objects.create(
                actor_account=account,
                actor_kind="platform_admin" if account.platform_role == "platform_admin" else "instructor",
                action="auth.profile_update",
                outcome="allowed",
                target_type="instructor_account",
                target_id=str(account.public_id),
                request_id=uuid.uuid4().hex,
                bounded_metadata={"changed_fields": ["display_name"]},
            )
    return no_store_json(_account_payload(account))


@csrf_protect
def instructor_password_view(request):
    if request.method != "PATCH":
        return _method_not_allowed(["PATCH"])
    session = resolve_instructor_session(request)
    if session is None:
        return _authentication_required()
    credentials = _strict_password_change_body(request)
    if credentials is None:
        return no_store_json({"error": "invalid_request"}, status=400)
    current_password, new_password = credentials

    with transaction.atomic():
        account = InstructorAccount.objects.select_for_update().select_related("user").get(
            pk=session.account_id
        )
        locked_session = InstructorSession.objects.select_for_update().get(pk=session.pk)
        if (
            locked_session.revoked_at is not None
            or locked_session.expires_at <= timezone.now()
            or not account.is_active
            or not account.user.is_active
        ):
            return _authentication_required()
        try:
            consume_login_attempt(account.email, request.META.get("REMOTE_ADDR", "unknown"))
        except LoginRateLimited as error:
            response = no_store_json({"error": "rate_limited"}, status=429)
            response["Retry-After"] = str(error.retry_after)
            return response
        if not account.user.check_password(current_password):
            return no_store_json({"error": "invalid_credentials"}, status=400)
        if account.user.check_password(new_password):
            return no_store_json({"error": "weak_password"}, status=400)
        try:
            validate_password(new_password, user=account.user)
        except ValidationError:
            return no_store_json({"error": "weak_password"}, status=400)

        account.user.set_password(new_password)
        account.user.save(update_fields=["password"])
        account.must_change_password = False
        account.save(update_fields=["must_change_password"])
        InstructorSession.objects.filter(account=account, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        _, expires_at, new_session = create_instructor_session(account)
        AuditEvent.objects.create(
            actor_account=account,
            actor_kind=(
                "platform_admin" if account.platform_role == "platform_admin" else "instructor"
            ),
            action="auth.password_change",
            outcome="allowed",
            target_type="instructor_session",
            target_id=str(new_session.pk),
            request_id=uuid.uuid4().hex,
            bounded_metadata={},
        )
    bind_browser_session(request, new_session)
    return no_store_json({
        "expires_at": expires_at.isoformat(),
        "must_change_password": False,
    })
