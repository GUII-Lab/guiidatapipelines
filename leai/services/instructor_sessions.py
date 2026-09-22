"""Opaque, revocable instructor bearer capabilities."""

import hashlib
import re
import secrets
from datetime import timedelta

from django.utils import timezone

from leai.models import InstructorSession


SESSION_LIFETIME = timedelta(hours=12)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def token_digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_instructor_session(account):
    token = secrets.token_urlsafe(32)
    expires_at = timezone.now() + SESSION_LIFETIME
    session = InstructorSession.objects.create(
        account=account,
        capability_digest=token_digest(token),
        expires_at=expires_at,
    )
    return token, expires_at, session


def bearer_token(request):
    authorization = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return None
    token = authorization[len(prefix):]
    return token if TOKEN_PATTERN.fullmatch(token) else None


def resolve_instructor_session(request, *, allow_revoked=False):
    token = bearer_token(request)
    if token is None:
        return None
    session = (
        InstructorSession.objects.select_related("account__user")
        .filter(capability_digest=token_digest(token))
        .first()
    )
    if session is None or session.expires_at <= timezone.now():
        return None
    if not allow_revoked and session.revoked_at is not None:
        return None
    if not allow_revoked and (
        not session.account.is_active or not session.account.user.is_active
    ):
        return None
    return session
