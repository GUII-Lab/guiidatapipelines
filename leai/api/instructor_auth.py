"""Strict JSON and bearer-only instructor authentication endpoints."""

import json
import uuid

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from leai.models import AuditEvent, InstructorAccount, InstructorSession
from leai.services.instructor_sessions import (
    create_instructor_session,
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


@csrf_exempt
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
        try:
            account = InstructorAccount.objects.select_related("user").get(email__iexact=email)
        except (InstructorAccount.DoesNotExist, InstructorAccount.MultipleObjectsReturned):
            check_password(password, _UNKNOWN_ACCOUNT_HASH)
            return _invalid_credentials()
        if (
            not account.user.check_password(password)
            or not account.is_active
            or not account.user.is_active
        ):
            return _invalid_credentials()
        with transaction.atomic():
            token, expires_at, session = create_instructor_session(account)
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
        return no_store_json(
            {
                "token": token,
                "expires_at": expires_at.isoformat(),
                "must_change_password": account.must_change_password,
            },
            status=201,
        )
    if request.method == "DELETE":
        session = resolve_instructor_session(request, allow_revoked=True)
        if session is None:
            return _authentication_required()
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
        response = HttpResponse(status=204)
        response["Cache-Control"] = "no-store"
        return response
    return _method_not_allowed(["POST", "DELETE"])


@csrf_exempt
def instructor_me_view(request):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    session = resolve_instructor_session(request)
    if session is None:
        return _authentication_required()
    account = session.account
    return no_store_json(
        {
            "id": str(account.public_id),
            "email": account.email,
            "display_name": account.display_name,
            "must_change_password": account.must_change_password,
            "platform_role": account.platform_role,
        }
    )
