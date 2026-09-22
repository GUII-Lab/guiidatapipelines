"""PostgreSQL-backed login attempt limits without raw client identifiers."""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from leai.models import InstructorLoginThrottle


WINDOW = timedelta(minutes=15)
EMAIL_LIMIT = 8
SOURCE_LIMIT = 120


class LoginRateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def _window_start(now):
    seconds = int(WINDOW.total_seconds())
    return datetime.fromtimestamp(
        int(now.timestamp()) // seconds * seconds,
        tz=datetime_timezone.utc,
    )


def _bucket_digest(scope, value, start):
    message = f"{scope}\0{value}\0{int(start.timestamp())}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


@transaction.atomic
def consume_login_attempt(email, source_address, *, now=None):
    """Debit both buckets in digest order, rolling back both on denial."""
    now = now or timezone.now()
    start = _window_start(now)
    retry_after = max(1, int(((start + WINDOW) - now).total_seconds()) + 1)
    buckets = sorted((
        (_bucket_digest("email", email.casefold(), start), EMAIL_LIMIT),
        (_bucket_digest("source", source_address or "unknown", start), SOURCE_LIMIT),
    ))
    for key_digest, limit in buckets:
        bucket, _ = InstructorLoginThrottle.objects.get_or_create(
            key_digest=key_digest,
            defaults={"window_start": start, "attempts": 0},
        )
        locked = InstructorLoginThrottle.objects.select_for_update().get(pk=bucket.pk)
        if locked.attempts >= limit:
            raise LoginRateLimited(retry_after)
        locked.attempts += 1
        locked.save(update_fields=["attempts"])
