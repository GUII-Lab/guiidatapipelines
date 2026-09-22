import hashlib
import json
import re
from collections.abc import Callable

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from leai.models.responses import MutationReceipt


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SCOPE_LIMITS = {
    "principal_scope": 255,
    "operation": 128,
    "target_key": 255,
}
_MAX_IDEMPOTENCY_KEY_CHARACTERS = 1024


class IdempotencyConflict(Exception):
    """The receipt key exists for a different canonical request."""


class InvalidMutationResult(ValueError):
    """The mutation callback returned an invalid or oversized result."""


class UnsupportedTransactionIsolation(RuntimeError):
    """Receipt replay requires PostgreSQL READ COMMITTED isolation."""


def _validate_inputs(
    *,
    principal_scope: str,
    operation: str,
    target_key: str,
    idempotency_key: str,
    request_hash: str,
) -> None:
    for field_name, max_length in _SCOPE_LIMITS.items():
        value = {
            "principal_scope": principal_scope,
            "operation": operation,
            "target_key": target_key,
        }[field_name]
        if not isinstance(value, str) or not value or len(value) > max_length:
            raise ValueError(
                f"{field_name} must be a non-empty string of at most "
                f"{max_length} characters"
            )

    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key
        or len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_CHARACTERS
    ):
        raise ValueError(
            "idempotency_key must be a non-empty string of at most "
            f"{_MAX_IDEMPOTENCY_KEY_CHARACTERS} characters"
        )
    if not isinstance(request_hash, str) or not _SHA256_PATTERN.fullmatch(
        request_hash
    ):
        raise ValueError("request_hash must be a lowercase SHA-256 hex digest")


def _canonicalize_result(result: dict) -> dict:
    if not isinstance(result, dict):
        raise InvalidMutationResult("mutation result must be a JSON object")
    try:
        serialized = json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise InvalidMutationResult(
            "mutation result must contain only canonical JSON values"
        ) from error

    # The database check uses this same function after jsonb has normalized
    # numbers, so Python's exponent formatting cannot undercount the result.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT leai_jsonb_canonical_size(candidate), candidate::text
            FROM (SELECT %s::jsonb AS candidate) AS normalized
            """,
            [serialized],
        )
        canonical_size, normalized_json = cursor.fetchone()

    if canonical_size > MutationReceipt.MAX_RESULT_BYTES:
        raise InvalidMutationResult(
            "mutation result exceeds the 16 KiB canonical JSON limit"
        )
    return json.loads(normalized_json)


def _replay_or_conflict(
    receipt: MutationReceipt,
    *,
    request_hash: str,
) -> tuple[dict, bool]:
    if receipt.request_hash != request_hash:
        raise IdempotencyConflict(
            "idempotency key was already used for a different request"
        )
    return receipt.result, True


def execute_once(
    *,
    principal_scope: str,
    operation: str,
    target_key: str,
    idempotency_key: str,
    request_hash: str,
    mutate: Callable[[], dict],
) -> tuple[dict, bool]:
    """Commit a mutation and its receipt together under READ COMMITTED.

    The unique-key loser must see the winner's committed row in a fresh
    statement snapshot. Any caller's outer transaction must also use READ
    COMMITTED; this service does not retry an outer transaction.
    """
    _validate_inputs(
        principal_scope=principal_scope,
        operation=operation,
        target_key=target_key,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if not callable(mutate):
        raise TypeError("mutate must be callable")

    idempotency_key_hash = hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()
    lookup = {
        "principal_scope": principal_scope,
        "operation": operation,
        "target_key": target_key,
        "idempotency_key_hash": idempotency_key_hash,
    }

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            isolation_level = cursor.fetchone()[0]
        if isolation_level != "read committed":
            raise UnsupportedTransactionIsolation(
                "execute_once requires PostgreSQL READ COMMITTED isolation"
            )

        receipt = (
            MutationReceipt.objects.select_for_update().filter(**lookup).first()
        )
        if receipt is not None:
            return _replay_or_conflict(receipt, request_hash=request_hash)

        try:
            with transaction.atomic():
                receipt = MutationReceipt.objects.create(
                    **lookup,
                    request_hash=request_hash,
                )
        except IntegrityError:
            try:
                receipt = MutationReceipt.objects.select_for_update().get(**lookup)
            except MutationReceipt.DoesNotExist:
                raise
            return _replay_or_conflict(receipt, request_hash=request_hash)

        result = _canonicalize_result(mutate())
        receipt.result = result
        receipt.completed_at = timezone.now()
        receipt.save(update_fields=("result", "completed_at"))
        return result, False
