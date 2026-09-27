from copy import deepcopy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher
from django.conf import settings
from django.db import connections, transaction
from django.test import Client, TestCase
from django.test import TransactionTestCase
from django.test import override_settings
from django.utils import timezone

from leai.models import AuditEvent, InstructorAccount, InstructorLoginThrottle, InstructorSession
from leai.api.instructor_auth import _UNKNOWN_ACCOUNT_HASH
from leai.services.login_throttle import LoginRateLimited, consume_login_attempt
from leai.tests.session_client import SessionClient


ROOT = "/datapipeline/api/v1/"


class InstructorAuthenticationApiTests(TestCase):
    def test_cors_allows_approved_app_origin_but_not_arbitrary_origin(self):
        for origin, allowed in (
            ("https://guii-lab.github.io", True),
            ("http://127.0.0.1:4173", True),
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
        with override_settings(LEAI_ENVIRONMENT="local", LEAI_BUILD_ID="local-backend"):
            csrf = self.client.get(ROOT + "instructor_csrf/").json()["csrf_token"]
        self.client.defaults["HTTP_X_CSRFTOKEN"] = csrf
        response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "environment_unavailable"})
        self.assertEqual(InstructorSession.objects.count(), 0)

    def test_unknown_account_uses_a_real_dummy_password_hash(self):
        self.assertIsNotNone(identify_hasher(_UNKNOWN_ACCOUNT_HASH))

    def setUp(self):
        self.client = SessionClient()
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

    def test_login_returns_only_metadata_and_issues_a_server_side_session(self):
        response = self.login(email="Teacher@UCSC.edu")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response["Cache-Control"], "no-store")
        payload = response.json()
        self.assertEqual(set(payload), {"expires_at", "must_change_password"})
        self.assertFalse(payload["must_change_password"])
        session = InstructorSession.objects.get(account=self.account)
        self.assertEqual(self.client.session["leai_instructor_session_id"], session.pk)
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertGreater(session.expires_at, timezone.now())
        self.assertLessEqual(session.expires_at, timezone.now() + timedelta(hours=1))
        audit = AuditEvent.objects.get(action="auth.login")
        self.assertEqual(audit.actor_account, self.account)
        self.assertEqual(audit.target_id, str(session.pk))
        self.assertTrue(audit.request_id)
        self.assertNotIn(cookie.value, str(audit.bounded_metadata))

    def test_password_change_is_optional_and_still_rotates_and_revokes_sessions(self):
        self.account.must_change_password = True
        self.account.save(update_fields=["must_change_password"])
        self.login()
        old_browser = SessionClient()
        old_browser.cookies = deepcopy(self.client.cookies)
        response = self.client.patch(
            ROOT + "instructor_password/",
            data=json.dumps({
                "current_password": "Test-Password-Only-2026!",
                "new_password": "New-Password-Only-2026!",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"expires_at", "must_change_password"})
        self.assertFalse(payload["must_change_password"])
        self.assertNotEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                            old_browser.cookies[settings.SESSION_COOKIE_NAME].value)
        self.assertEqual(
            old_browser.get(
                ROOT + "instructor_me/",
            ).status_code,
            401,
        )
        me = self.client.get(
            ROOT + "instructor_me/",
        )
        self.assertEqual(me.status_code, 200)
        self.assertFalse(me.json()["must_change_password"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("New-Password-Only-2026!"))

    def test_login_ignores_legacy_forced_password_change_flag(self):
        self.account.must_change_password = True
        self.account.save(update_fields=["must_change_password"])

        client = Client()
        response = client.post(
            ROOT + "instructor_sessions/",
            data=json.dumps({"email": self.user.email, "password": "Test-Password-Only-2026!"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["must_change_password"])
        me = client.get(ROOT + "instructor_me/")
        self.assertEqual(me.status_code, 200)
        self.assertFalse(me.json()["must_change_password"])

    def test_password_change_rejects_wrong_current_password(self):
        self.login()
        response = self.client.patch(
            ROOT + "instructor_password/",
            data=json.dumps({"current_password": "wrong", "new_password": "New-Password-Only-2026!"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_credentials"})
        self.assertTrue(self.user.check_password("Test-Password-Only-2026!"))

    def test_password_change_rejects_weak_and_reused_passwords(self):
        self.login()
        for new_password in ("password", "Test-Password-Only-2026!"):
            with self.subTest(new_password=new_password):
                response = self.client.patch(
                    ROOT + "instructor_password/",
                    data=json.dumps({
                        "current_password": "Test-Password-Only-2026!",
                        "new_password": new_password,
                    }),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"error": "weak_password"})
        self.assertEqual(
            self.client.get(ROOT + "instructor_me/").status_code,
            200,
        )

    def test_password_change_requires_cookie_and_revokes_all_existing_sessions(self):
        self.login()
        first = SessionClient()
        first.cookies = deepcopy(self.client.cookies)
        self.login()
        second = SessionClient()
        second.cookies = deepcopy(self.client.cookies)
        old_secrets = [browser.cookies[settings.SESSION_COOKIE_NAME].value for browser in (first, second)]
        body = json.dumps({
            "current_password": "Test-Password-Only-2026!",
            "new_password": "New-Password-Only-2026!",
        })
        unauthenticated = SessionClient().patch(
            ROOT + "instructor_password/", data=body, content_type="application/json"
        )
        self.assertEqual(unauthenticated.status_code, 401)
        changed = self.client.patch(
            ROOT + "instructor_password/", data=body, content_type="application/json",
        )
        self.assertEqual(changed.status_code, 200)
        for browser in (first, second):
            self.assertEqual(
                browser.get(ROOT + "instructor_me/").status_code,
                401,
            )
        audit = AuditEvent.objects.get(action="auth.password_change")
        self.assertEqual(audit.actor_account, self.account)
        for secret in old_secrets:
            self.assertNotIn(secret, str(audit.bounded_metadata))

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

    def test_me_and_logout_use_cookie_and_logout_retry_is_idempotent(self):
        self.login()
        self.assertEqual(SessionClient().get(ROOT + "instructor_me/").status_code, 401)
        me = self.client.get(ROOT + "instructor_me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], self.account.email)
        self.assertEqual(me["Cache-Control"], "no-store")
        first = self.client.delete(ROOT + "instructor_sessions/")
        retry = self.client.delete(ROOT + "instructor_sessions/")
        self.assertEqual((first.status_code, retry.status_code), (204, 204))
        self.assertEqual(AuditEvent.objects.filter(action="auth.logout").count(), 1)
        self.assertTrue(AuditEvent.objects.get(action="auth.logout").request_id)
        self.assertEqual(self.client.get(ROOT + "instructor_me/").status_code, 401)

    def test_recognized_logout_retry_stays_idempotent_after_account_deactivation(self):
        self.login()
        self.assertEqual(self.client.delete(ROOT + "instructor_sessions/").status_code, 204)
        self.account.is_active = False
        self.account.save(update_fields=["is_active"])
        self.assertEqual(self.client.delete(ROOT + "instructor_sessions/").status_code, 204)
        self.assertEqual(AuditEvent.objects.filter(action="auth.logout").count(), 1)

    def test_expired_and_unknown_bearer_tokens_are_generic_unauthorized(self):
        self.login()
        InstructorSession.objects.filter(account=self.account).update(expires_at=timezone.now())
        self.assertEqual(self.client.get(ROOT + "instructor_me/").status_code, 401)
        for credential in ("unknown-token", ""):
            response = self.client.get(
                ROOT + "instructor_me/",
                HTTP_AUTHORIZATION=f"Bearer {credential}",
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"error": "authentication_required"})


class PasswordChangeConcurrencyTests(TransactionTestCase):
    def test_old_password_cannot_issue_a_session_during_password_rotation(self):
        user_type = get_user_model()
        user = user_type.objects.create_user(
            username="rotation-instructor", email="rotation@ucsc.edu",
            password="Old-Password-Only-2026!",
        )
        account = InstructorAccount.objects.create(
            user=user, email="rotation@ucsc.edu", display_name="Rotation Instructor",
            must_change_password=False,
        )
        old_password_checked = Event()
        release_login = Event()
        rotation_committed = Event()
        body = json.dumps({
            "email": "rotation@ucsc.edu", "password": "Old-Password-Only-2026!",
        })

        original_check = user_type.check_password

        def held_check_password(instance, password):
            result = original_check(instance, password)
            if instance.pk == user.pk and password == "Old-Password-Only-2026!":
                old_password_checked.set()
                if not release_login.wait(10):
                    raise AssertionError("Login was never released")
            return result

        def login():
            try:
                return Client().post(
                    ROOT + "instructor_sessions/", data=body,
                    content_type="application/json",
                )
            finally:
                connections.close_all()

        def rotate_password():
            try:
                with transaction.atomic():
                    locked = InstructorAccount.objects.select_for_update().get(pk=account.pk)
                    rotation_user = user_type.objects.get(pk=locked.user_id)
                    rotation_user.set_password("New-Password-Only-2026!")
                    rotation_user.save(update_fields=["password"])
                    InstructorSession.objects.filter(account=locked, revoked_at__isnull=True).update(
                        revoked_at=timezone.now()
                    )
                rotation_committed.set()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(user_type, "check_password", held_check_password):
                login_future = pool.submit(login)
                try:
                    self.assertTrue(old_password_checked.wait(5))
                    rotation_future = pool.submit(rotate_password)
                    rotation_committed.wait(3)
                finally:
                    release_login.set()
                login_future.result(timeout=10)
                rotation_future.result(timeout=10)

        self.assertEqual(
            InstructorSession.objects.filter(account=account, revoked_at__isnull=True).count(), 0
        )


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
