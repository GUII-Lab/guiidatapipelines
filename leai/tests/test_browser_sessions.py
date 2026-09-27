"""The browser must never receive an instructor bearer credential."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from leai.models import InstructorAccount, InstructorSession


ROOT = '/datapipeline/api/v1/'


class BrowserSessionTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.user = get_user_model().objects.create_user(
            username='cookie-test', email='cookie-test@ucsc.edu', password='Browser-Test-Only-2026!',
        )
        self.account = InstructorAccount.objects.create(
            user=self.user, email=self.user.email, display_name='Cookie Test', must_change_password=False,
        )

    def csrf(self, client=None):
        response = (client or self.client).get(ROOT + 'instructor_csrf/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')
        return response.json()['csrf_token']

    def login(self, client=None):
        client = client or self.client
        return client.post(ROOT + 'instructor_sessions/', {
            'email': self.user.email, 'password': 'Browser-Test-Only-2026!',
        }, content_type='application/json', HTTP_X_CSRFTOKEN=self.csrf(client))

    def test_login_uses_an_httponly_cookie_and_never_returns_a_bearer(self):
        response = self.login()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.json()), {'expires_at', 'must_change_password'})
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')
        self.assertEqual(cookie['path'], '/')
        self.assertEqual(cookie['domain'], '')
        self.assertEqual(self.client.get(ROOT + 'instructor_me/').status_code, 200)
        self.assertEqual(Client().get(ROOT + 'instructor_me/').status_code, 401)

    def test_login_requires_csrf_and_rejects_an_untrusted_origin(self):
        payload = {'email': self.user.email, 'password': 'Browser-Test-Only-2026!'}
        missing = self.client.post(ROOT + 'instructor_sessions/', payload, content_type='application/json')
        self.assertEqual(missing.status_code, 403)
        foreign = self.client.post(ROOT + 'instructor_sessions/', payload, content_type='application/json',
                                   HTTP_X_CSRFTOKEN=self.csrf(), HTTP_ORIGIN='https://evil.example')
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(InstructorSession.objects.count(), 0)

    def test_reauthentication_rotates_even_an_already_authenticated_browser(self):
        self.login()
        old_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        replay = Client()
        replay.cookies[settings.SESSION_COOKIE_NAME] = old_key
        self.assertEqual(self.login().status_code, 201)
        self.assertNotEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, old_key)
        self.assertEqual(replay.get(ROOT + 'instructor_me/').status_code, 401)
        self.assertEqual(self.client.get(ROOT + 'instructor_me/').status_code, 200)

    def test_all_instructor_mutations_require_csrf_even_with_a_valid_session(self):
        self.assertEqual(self.login().status_code, 201)
        for path, method in (
            ('instructor_sessions/', 'delete'),
            ('instructor_password/', 'patch'),
            ('instructor_courses/11111111-1111-4111-8111-111111111111/debug-settings/', 'patch'),
            ('instructor_courses/11111111-1111-4111-8111-111111111111/responses/search/', 'post'),
        ):
            with self.subTest(path=path):
                response = getattr(self.client, method)(ROOT + path, {}, content_type='application/json')
                self.assertEqual(response.status_code, 403)

    def test_logout_flushes_the_cookie_session_and_revokes_server_record(self):
        self.login()
        replay = Client()
        replay.cookies[settings.SESSION_COOKIE_NAME] = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        response = self.client.delete(ROOT + 'instructor_sessions/', HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(response.status_code, 204)
        self.assertIsNotNone(InstructorSession.objects.get().revoked_at)
        self.assertEqual(replay.get(ROOT + 'instructor_me/').status_code, 401)
        self.assertEqual(self.client.get(ROOT + 'instructor_me/').status_code, 401)

    def test_expiry_and_account_disable_are_checked_on_each_request(self):
        self.login()
        InstructorSession.objects.update(expires_at=timezone.now())
        self.assertEqual(self.client.get(ROOT + 'instructor_me/').status_code, 401)
        self.login()
        self.account.is_active = False
        self.account.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(ROOT + 'instructor_me/').status_code, 401)

    def test_password_change_rotates_session_and_invalidates_other_browsers(self):
        other = Client(enforce_csrf_checks=True)
        self.login()
        self.login(other)
        old_cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        response = self.client.patch(ROOT + 'instructor_password/', {
            'current_password': 'Browser-Test-Only-2026!', 'new_password': 'Rotated-Browser-Test-2026!',
        }, content_type='application/json', HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('token', response.json())
        self.assertNotEqual(old_cookie, self.client.cookies[settings.SESSION_COOKIE_NAME].value)
        self.assertEqual(self.client.get(ROOT + 'instructor_me/').status_code, 200)
        self.assertEqual(other.get(ROOT + 'instructor_me/').status_code, 401)
