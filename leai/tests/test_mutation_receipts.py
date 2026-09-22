import hashlib
import json
import queue
import threading
import time

from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TransactionTestCase
from django.utils import timezone

from leai.models.identity import Institution
from leai.models.responses import MutationReceipt
from leai.services.mutation_receipts import (
    IdempotencyConflict,
    InvalidMutationResult,
    execute_once,
)


class CallbackFailure(Exception):
    pass


class MutationReceiptTests(TransactionTestCase):
    thread_timeout_seconds = 8

    def _receipt_values(self, **overrides):
        values = {
            "principal_scope": "occurrence:1",
            "operation": "allocate",
            "target_key": "occurrence:1",
            "idempotency_key_hash": hashlib.sha256(b"request-0001").hexdigest(),
            "request_hash": "a" * 64,
            "result": {"ok": True},
            "completed_at": timezone.now(),
        }
        values.update(overrides)
        return values

    def test_identical_retry_returns_original_result_once(self):
        calls = []
        first, replayed_first = execute_once(
            principal_scope="occurrence:1",
            operation="allocate",
            target_key="occurrence:1",
            idempotency_key="request-0001",
            request_hash="a" * 64,
            mutate=lambda: calls.append("called") or {"session": "abc"},
        )
        second, replayed_second = execute_once(
            principal_scope="occurrence:1",
            operation="allocate",
            target_key="occurrence:1",
            idempotency_key="request-0001",
            request_hash="a" * 64,
            mutate=lambda: calls.append("called-again") or {"session": "def"},
        )

        self.assertEqual(first, {"session": "abc"})
        self.assertEqual(first, second)
        self.assertEqual(calls, ["called"])
        self.assertFalse(replayed_first)
        self.assertTrue(replayed_second)

    def test_same_key_different_hash_conflicts_without_mutating(self):
        calls = []
        execute_once(
            principal_scope="occurrence:1",
            operation="allocate",
            target_key="occurrence:1",
            idempotency_key="request-0001",
            request_hash="a" * 64,
            mutate=lambda: calls.append("first") or {"ok": True},
        )

        with self.assertRaises(IdempotencyConflict):
            execute_once(
                principal_scope="occurrence:1",
                operation="allocate",
                target_key="occurrence:1",
                idempotency_key="request-0001",
                request_hash="b" * 64,
                mutate=lambda: calls.append("conflict") or {"ok": False},
            )

        self.assertEqual(calls, ["first"])

    def test_callback_exception_rolls_back_domain_write_and_allows_retry(self):
        calls = []

        def fail_after_write():
            calls.append("failed")
            Institution.objects.create(slug="retry-safe", name="Retry Safe")
            raise CallbackFailure("mutation failed")

        with self.assertRaises(CallbackFailure):
            execute_once(
                principal_scope="service:test",
                operation="create-institution",
                target_key="institution:retry-safe",
                idempotency_key="request-rollback",
                request_hash="c" * 64,
                mutate=fail_after_write,
            )

        self.assertFalse(Institution.objects.filter(slug="retry-safe").exists())
        self.assertEqual(MutationReceipt.objects.count(), 0)

        def retry_write():
            calls.append("retried")
            institution = Institution.objects.create(
                slug="retry-safe",
                name="Retry Safe",
            )
            return {"institution_id": institution.id}

        result, replayed = execute_once(
            principal_scope="service:test",
            operation="create-institution",
            target_key="institution:retry-safe",
            idempotency_key="request-rollback",
            request_hash="c" * 64,
            mutate=retry_write,
        )

        self.assertEqual(calls, ["failed", "retried"])
        self.assertFalse(replayed)
        self.assertEqual(result["institution_id"], Institution.objects.get().id)
        self.assertEqual(MutationReceipt.objects.count(), 1)

    def test_result_accepts_exactly_16_kib_of_canonical_json(self):
        object_overhead = len(b'{"value":""}')
        result_value = "x" * (16 * 1024 - object_overhead)
        expected_bytes = json.dumps(
            {"value": result_value},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(len(expected_bytes), 16 * 1024)

        result, replayed = execute_once(
            principal_scope="service:test",
            operation="bounded-result",
            target_key="target:exact-limit",
            idempotency_key="request-exact-limit",
            request_hash="d" * 64,
            mutate=lambda: {"value": result_value},
        )

        self.assertFalse(replayed)
        self.assertEqual(result, {"value": result_value})

    def test_result_over_16_kib_is_rejected_and_reservation_rolls_back(self):
        object_overhead = len(b'{"value":""}')
        result_value = "x" * (16 * 1024 - object_overhead + 1)

        with self.assertRaises(InvalidMutationResult):
            execute_once(
                principal_scope="service:test",
                operation="bounded-result",
                target_key="target:over-limit",
                idempotency_key="request-over-limit",
                request_hash="e" * 64,
                mutate=lambda: {"value": result_value},
            )

        self.assertFalse(MutationReceipt.objects.exists())

    def test_result_limit_counts_utf8_bytes_not_characters(self):
        with self.assertRaises(InvalidMutationResult):
            execute_once(
                principal_scope="service:test",
                operation="bounded-result",
                target_key="target:utf8-limit",
                idempotency_key="request-utf8-limit",
                request_hash="f" * 64,
                mutate=lambda: {"value": "é" * 8190},
            )

    def test_database_rejects_malformed_hashes(self):
        for field_name in ("idempotency_key_hash", "request_hash"):
            with self.subTest(field_name=field_name):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    MutationReceipt.objects.create(
                        **self._receipt_values(**{field_name: "not-a-sha256"})
                    )

    def test_database_rejects_empty_opaque_scope_values(self):
        for field_name in ("principal_scope", "operation", "target_key"):
            with self.subTest(field_name=field_name):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    MutationReceipt.objects.create(
                        **self._receipt_values(**{field_name: ""})
                    )

    def test_database_rejects_non_object_and_oversized_results(self):
        invalid_results = (
            [],
            {"value": "x" * (16 * 1024)},
        )
        for invalid_result in invalid_results:
            with self.subTest(result_type=type(invalid_result).__name__):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    MutationReceipt.objects.create(
                        **self._receipt_values(result=invalid_result)
                    )

    def test_database_rejects_committed_incomplete_receipt(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            MutationReceipt.objects.create(
                **self._receipt_values(result=None, completed_at=None)
            )

    def test_concurrent_identical_requests_use_distinct_connections_and_mutate_once(self):
        outcomes = queue.Queue()
        pids = {}
        calls = []
        calls_lock = threading.Lock()
        first_mutating = threading.Event()
        second_connection_ready = threading.Event()
        allow_first_to_finish = threading.Event()

        def record_call(label):
            with calls_lock:
                calls.append(label)

        def run_request(label, hold_callback=False):
            close_old_connections()
            database = connections["default"]
            try:
                database.ensure_connection()
                with database.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '8s'")
                    pids[label] = database.connection.get_backend_pid()
                if label == "second":
                    second_connection_ready.set()

                def mutate():
                    record_call(label)
                    if hold_callback:
                        first_mutating.set()
                        if not allow_first_to_finish.wait(self.thread_timeout_seconds):
                            raise CallbackFailure("test did not release first mutation")
                    return {"winner": "stable"}

                result, replayed = execute_once(
                    principal_scope="occurrence:concurrent",
                    operation="allocate",
                    target_key="occurrence:concurrent",
                    idempotency_key="request-concurrent",
                    request_hash="1" * 64,
                    mutate=mutate,
                )
                outcomes.put((label, "ok", result, replayed))
            except IdempotencyConflict as error:
                outcomes.put((label, "conflict", str(error)))
            except DatabaseError as error:
                cause = getattr(error, "__cause__", None)
                outcomes.put(
                    (
                        label,
                        "database_error",
                        getattr(cause, "pgcode", None),
                        repr(error),
                    )
                )
            except Exception as error:
                outcomes.put((label, "unexpected_error", repr(error)))
            finally:
                close_old_connections()

        first_thread = threading.Thread(
            target=run_request,
            args=("first", True),
            name="mutation-receipt-first",
        )
        second_thread = threading.Thread(
            target=run_request,
            args=("second",),
            name="mutation-receipt-second",
        )

        first_thread.start()
        self.assertTrue(
            first_mutating.wait(self.thread_timeout_seconds),
            "first request never reached its mutation callback",
        )
        second_thread.start()
        self.assertTrue(
            second_connection_ready.wait(self.thread_timeout_seconds),
            "second request never established its PostgreSQL connection",
        )

        lock_wait_observed = False
        wait_deadline = time.monotonic() + self.thread_timeout_seconds
        try:
            while time.monotonic() < wait_deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT wait_event_type
                        FROM pg_stat_activity
                        WHERE pid = %s
                        """,
                        [pids["second"]],
                    )
                    row = cursor.fetchone()
                if row and row[0] == "Lock":
                    lock_wait_observed = True
                    break
                time.sleep(0.01)
        finally:
            allow_first_to_finish.set()

        first_thread.join(self.thread_timeout_seconds)
        second_thread.join(self.thread_timeout_seconds)

        self.assertFalse(first_thread.is_alive(), "first request deadlocked or timed out")
        self.assertFalse(second_thread.is_alive(), "second request deadlocked or timed out")
        self.assertTrue(
            lock_wait_observed,
            "second PostgreSQL connection never waited on the first transaction",
        )
        self.assertNotEqual(pids["first"], pids["second"])

        results = [
            outcomes.get(timeout=self.thread_timeout_seconds),
            outcomes.get(timeout=self.thread_timeout_seconds),
        ]
        self.assertEqual(
            [outcome[1] for outcome in results],
            ["ok", "ok"],
            f"conflict/deadlock/timeout classification: {results!r}",
        )
        self.assertEqual({outcome[3] for outcome in results}, {False, True})
        self.assertEqual({json.dumps(outcome[2], sort_keys=True) for outcome in results}, {'{"winner": "stable"}'})
        self.assertEqual(calls, ["first"])
        self.assertEqual(MutationReceipt.objects.count(), 1)
        self.assertEqual(
            MutationReceipt.objects.get().result,
            {"winner": "stable"},
        )
