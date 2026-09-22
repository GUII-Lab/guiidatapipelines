import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher
from django.test import Client, TestCase
from django.test import TransactionTestCase
from django.test import override_settings
from django.utils import timezone

from leai.models import AuditEvent, InstructorAccount, InstructorLoginThrottle, InstructorSession
from leai.api.instructor_auth import _UNKNOWN_ACCOUNT_HASH
from leai.services.login_throttle import LoginRateLimited, consume_login_attempt


ROOT = "/datapipeline/api/v1/"


class InstructorAuthenticationApiTests(TestCase):
    def test_cors_allows_approved_app_origin_but_not_arbitrary_origin(self):
        for origin, allowed in (
            ("https://guii-lab.github.io", True),
            ("https://unapproved.example", False),
        ):
            with self.subTest(origin=origin):
                response = self.client.options(
                    ROOT + "instructor_sessions/",
                    HTTP_ORIGIN=origin,
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                )
                if allowed:
                    self.assertEqual(response.get("Access-Control-Allow-Origin"), origin)
                else:
                    self.assertNotIn("Access-Control-Allow-Origin", response)

    @override_settings(LEAI_ENVIRONMENT="qa", LEAI_BUILD_ID="a" * 40)
    def test_schema_mismatch_blocks_login_before_creating_a_session(self):
        response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "environment_unavailable"})
        self.assertEqual(InstructorSession.objects.count(), 0)

    def test_unknown_account_uses_a_real_dummy_password_hash(self):
        self.assertIsNotNone(identify_hasher(_UNKNOWN_ACCOUNT_HASH))

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.user = get_user_model().objects.create_user(
            username="instructor",
            email="teacher@ucsc.edu",
            password="Test-Password-Only-2026!",
        )
        self.account = InstructorAccount.objects.create(
            user=self.user,
            email="teacher@ucsc.edu",
            display_name="Test Instructor",
            must_change_password=False,
        )

    def login(self, email="teacher@ucsc.edu", password="Test-Password-Only-2026!"):
        return self.client.post(
            ROOT + "instructor_sessions/",
            data=json.dumps({"email": email, "password": password}),
            content_type="application/json",
        )

    def test_login_returns_one_opaque_token_and_stores_only_its_digest(self):
        response = self.login(email="Teacher@UCSC.edu")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response["Cache-Control"], "no-store")
        payload = response.json()
        self.assertEqual(set(payload), {"token", "expires_at", "must_change_password"})
        self.assertFalse(payload["must_change_password"])
        self.assertGreaterEqual(len(payload["token"]), 32)
        session = InstructorSession.objects.get(account=self.account)
        self.assertNotEqual(session.capability_digest, payload["token"])
        self.assertEqual(
            session.capability_digest,
            hashlib.sha256(payload["token"].encode("ascii")).hexdigest(),
        )
        self.assertGreater(session.expires_at, timezone.now())
        audit = AuditEvent.objects.get(action="auth.login")
        self.assertEqual(audit.actor_account, self.account)
        self.assertEqual(audit.target_id, str(session.pk))
        self.assertTrue(audit.request_id)
        self.assertNotIn(payload["token"], str(audit.bounded_metadata))

    def test_unknown_wrong_and_inactive_credentials_are_indistinguishable(self):
        for email, password in (
            ("nobody@ucsc.edu", "Test-Password-Only-2026!"),
            ("teacher@ucsc.edu", "wrong"),
        ):
            with self.subTest(email=email):
                response = self.login(email=email, password=password)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"error": "invalid_credentials"})
                self.assertEqual(response["Cache-Control"], "no-store")
        self.account.is_active = False
        self.account.save(update_fields=["is_active"])
        self.assertEqual(self.login().json(), {"error": "invalid_credentials"})
        self.assertEqual(InstructorSession.objects.count(), 0)

    def test_repeated_login_attempts_are_limited_before_password_hash_work(self):
        for _ in range(8):
            self.assertEqual(self.login(password="wrong").status_code, 401)
        throttled = self.login(password="Test-Password-Only-2026!")
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(throttled.json(), {"error": "rate_limited"})
        self.assertTrue(throttled.get("Retry-After"))
        self.assertEqual(InstructorSession.objects.count(), 0)
        self.assertEqual(InstructorLoginThrottle.objects.count(), 2)
        for digest in InstructorLoginThrottle.objects.values_list("key_digest", flat=True):
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertNotIn("teacher@ucsc.edu", digest)

    def test_source_bucket_limits_password_spray_across_distinct_emails(self):
        with patch("leai.services.login_throttle.SOURCE_LIMIT", 2):
            for number in (1, 2):
                self.assertEqual(
                    self.login(email=f"nobody-{number}@ucsc.edu").status_code, 401
                )
            self.assertEqual(self.login(email="nobody-3@ucsc.edu").status_code, 429)

    def test_login_rejects_malformed_unknown_and_wrongly_typed_fields(self):
        for body in (
            "{bad",
            "[]",
            '{}',
            '{"email":"teacher@ucsc.edu","password":"x","extra":true}',
            '{"email":42,"password":"x"}',
            '{"email":"teacher@ucsc.edu","password":false}',
        ):
            with self.subTest(body=body):
                response = self.client.post(
                    ROOT + "instructor_sessions/",
                    data=body,
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"error": "invalid_request"})
                self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(InstructorSession.objects.count(), 0)

    def test_me_and_logout_use_bearer_only_and_logout_retry_is_idempotent(self):
        token = self.login().json()["token"]
        self.assertEqual(self.client.get(ROOT + "instructor_me/").status_code, 401)
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        me = self.client.get(ROOT + "instructor_me/", **headers)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], self.account.email)
        self.assertEqual(me["Cache-Control"], "no-store")
        first = self.client.delete(ROOT + "instructor_sessions/", **headers)
        retry = self.client.delete(ROOT + "instructor_sessions/", **headers)
        self.assertEqual((first.status_code, retry.status_code), (204, 204))
        self.assertEqual(AuditEvent.objects.filter(action="auth.logout").count(), 1)
        self.assertTrue(AuditEvent.objects.get(action="auth.logout").request_id)
        self.assertEqual(self.client.get(ROOT + "instructor_me/", **headers).status_code, 401)

    def test_recognized_logout_retry_stays_idempotent_after_account_deactivation(self):
        token = self.login().json()["token"]
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        self.assertEqual(self.client.delete(ROOT + "instructor_sessions/", **headers).status_code, 204)
        self.account.is_active = False
        self.account.save(update_fields=["is_active"])
        self.assertEqual(self.client.delete(ROOT + "instructor_sessions/", **headers).status_code, 204)
        self.assertEqual(AuditEvent.objects.filter(action="auth.logout").count(), 1)

    def test_expired_and_unknown_bearer_tokens_are_generic_unauthorized(self):
        token = self.login().json()["token"]
        InstructorSession.objects.filter(account=self.account).update(expires_at=timezone.now())
        for credential in (token, "unknown-token", ""):
            response = self.client.get(
                ROOT + "instructor_me/",
                HTTP_AUTHORIZATION=f"Bearer {credential}",
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"error": "authentication_required"})


class LoginThrottleConcurrencyTests(TransactionTestCase):
    def test_parallel_attempts_cannot_exceed_the_email_limit(self):
        barrier = Barrier(12)

        def attempt(_):
            barrier.wait(timeout=10)
            try:
                consume_login_attempt("parallel@ucsc.edu", "127.0.0.1")
                return "accepted"
            except LoginRateLimited:
                return "limited"

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(attempt, range(12)))
        self.assertEqual(results.count("accepted"), 8)
        self.assertEqual(results.count("limited"), 4)
        self.assertEqual(
            sorted(InstructorLoginThrottle.objects.values_list("attempts", flat=True)),
            [8, 8],
        )
