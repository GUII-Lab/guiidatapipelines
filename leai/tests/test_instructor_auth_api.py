import hashlib
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher
from django.test import Client, TestCase
from django.utils import timezone

from leai.models import AuditEvent, InstructorAccount, InstructorSession
from leai.api.instructor_auth import _UNKNOWN_ACCOUNT_HASH


ROOT = "/datapipeline/api/v1/"


class InstructorAuthenticationApiTests(TestCase):
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
        self.assertEqual(self.client.get(ROOT + "instructor_me/", **headers).status_code, 401)

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
