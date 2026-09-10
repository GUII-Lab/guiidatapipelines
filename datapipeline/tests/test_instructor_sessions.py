import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.utils import timezone

from datapipeline.models import (
    Course,
    InstructorAccount,
    InstructorAuditEvent,
    InstructorSession,
)


class InstructorSessionApiTests(TestCase):
    email = 'teacher@example.edu'
    temporary_password = 'TemporaryPass123!'

    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_user(
            username=self.email,
            email=self.email,
            password=self.temporary_password,
        )
        self.account = InstructorAccount.objects.create(
            user=self.user,
            email=self.email,
            display_name='Prof. Test',
        )

    def post_json(self, path, payload, token=None):
        headers = {}
        if token:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type='application/json',
            **headers,
        )

    def login(self, password=None):
        return self.post_json('/datapipeline/api/instructor_sessions/', {
            'email': self.email,
            'password': password or self.temporary_password,
        })

    def test_login_returns_raw_token_but_persists_only_its_digest(self):
        response = self.login()

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        raw_token = payload['token']
        stored = InstructorSession.objects.get()
        self.assertNotEqual(raw_token, stored.token_digest)
        self.assertEqual(
            hashlib.sha256(raw_token.encode('utf-8')).hexdigest(),
            stored.token_digest,
        )
        self.assertTrue(payload['must_change_password'])
        self.assertNotIn(raw_token, str(stored.__dict__))

    def test_successful_login_records_one_event_for_account_and_session(self):
        response = self.login()

        self.assertEqual(response.status_code, 201)
        raw_token = response.json()['token']
        event = InstructorAuditEvent.objects.get(
            action=InstructorAuditEvent.ACTION_LOGIN_SUCCEEDED,
        )
        self.assertEqual(event.actor, self.account)
        self.assertEqual(
            event.session.token_digest,
            hashlib.sha256(raw_token.encode('utf-8')).hexdigest(),
        )
        self.assertEqual(event.outcome, InstructorAuditEvent.OUTCOME_SUCCESS)
        self.assertEqual(event.metadata, {})

    def test_login_rolls_back_session_when_audit_write_fails(self):
        with patch(
            'datapipeline.instructor_views.record_instructor_event',
            side_effect=ValidationError('audit failed'),
        ):
            with self.assertRaises(ValidationError):
                self.login()

        self.assertFalse(InstructorSession.objects.exists())

    def test_unknown_email_and_wrong_password_return_same_error(self):
        unknown = self.post_json('/datapipeline/api/instructor_sessions/', {
            'email': 'unknown@example.edu',
            'password': self.temporary_password,
        })
        wrong = self.login(password='WrongPassword123!')

        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.json(), {'error': 'invalid_credentials'})
        self.assertEqual(wrong.json(), {'error': 'invalid_credentials'})

    def test_denied_logins_record_bounded_events_without_disclosing_email(self):
        unknown = self.post_json('/datapipeline/api/instructor_sessions/', {
            'email': 'unknown@example.edu',
            'password': self.temporary_password,
        })
        wrong = self.login(password='WrongPassword123!')

        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        events = list(InstructorAuditEvent.objects.filter(
            action=InstructorAuditEvent.ACTION_LOGIN_DENIED,
        ).order_by('occurred_at'))
        self.assertEqual(len(events), 2)
        self.assertIsNone(events[0].actor)
        self.assertEqual(events[0].metadata, {})
        self.assertEqual(events[1].actor, self.account)
        self.assertEqual(events[1].metadata, {})
        self.assertNotIn('unknown@example.edu', str(events[0].__dict__))

    def test_inactive_account_cannot_login(self):
        self.account.is_active = False
        self.account.save(update_fields=['is_active'])

        response = self.login()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'error': 'invalid_credentials'})
        self.assertFalse(InstructorSession.objects.exists())

    def test_current_account_requires_a_valid_bearer_token(self):
        response = self.client.get('/datapipeline/api/instructor_me/')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'error': 'authentication_required'})

    def test_current_account_returns_manual_unverified_identity(self):
        token = self.login().json()['token']

        response = self.client.get(
            '/datapipeline/api/instructor_me/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['email'], self.email)
        self.assertEqual(payload['display_name'], 'Prof. Test')
        self.assertEqual(payload['auth_provider'], 'manual')
        self.assertFalse(payload['email_verified'])
        self.assertTrue(payload['must_change_password'])
        self.assertEqual(payload['institutions'], [])
        self.assertEqual(payload['courses'], [])

    def test_expired_and_revoked_tokens_are_rejected(self):
        expired_token = self.login().json()['token']
        expired_session = InstructorSession.objects.get()
        expired_session.expires_at = timezone.now() - timedelta(seconds=1)
        expired_session.save(update_fields=['expires_at'])

        expired = self.client.get(
            '/datapipeline/api/instructor_me/',
            HTTP_AUTHORIZATION=f'Bearer {expired_token}',
        )
        self.assertEqual(expired.status_code, 401)

        revoked_token = self.login().json()['token']
        revoked_session = InstructorSession.objects.order_by('-id').first()
        revoked_session.revoked_at = timezone.now()
        revoked_session.save(update_fields=['revoked_at'])
        revoked = self.client.get(
            '/datapipeline/api/instructor_me/',
            HTTP_AUTHORIZATION=f'Bearer {revoked_token}',
        )
        self.assertEqual(revoked.status_code, 401)

    def test_logout_revokes_presented_session(self):
        token = self.login().json()['token']

        response = self.client.delete(
            '/datapipeline/api/instructor_sessions/current/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(response.status_code, 204)
        session = InstructorSession.objects.get()
        self.assertIsNotNone(session.revoked_at)

        event = InstructorAuditEvent.objects.get(
            action=InstructorAuditEvent.ACTION_LOGOUT,
        )
        self.assertEqual(event.actor, self.account)
        self.assertEqual(event.session, session)
        self.assertEqual(event.outcome, InstructorAuditEvent.OUTCOME_SUCCESS)

    def test_logout_rolls_back_revocation_when_audit_write_fails(self):
        token = self.login().json()['token']
        session = InstructorSession.objects.get()

        with patch(
            'datapipeline.instructor_views.record_instructor_event',
            side_effect=ValidationError('audit failed'),
        ):
            with self.assertRaises(ValidationError):
                self.client.delete(
                    '/datapipeline/api/instructor_sessions/current/',
                    HTTP_AUTHORIZATION=f'Bearer {token}',
                )

        session.refresh_from_db()
        self.assertIsNone(session.revoked_at)

    def test_password_change_rejects_wrong_current_password(self):
        token = self.login().json()['token']

        response = self.post_json(
            '/datapipeline/api/instructor_password/',
            {
                'current_password': 'WrongPassword123!',
                'new_password': 'ACompletelyNewPass123!',
            },
            token=token,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'error': 'invalid_current_password'})

    def test_password_change_rejects_weak_new_password(self):
        token = self.login().json()['token']

        response = self.post_json(
            '/datapipeline/api/instructor_password/',
            {
                'current_password': self.temporary_password,
                'new_password': 'password',
            },
            token=token,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'invalid_password')
        self.assertTrue(response.json()['details'])

    def test_password_change_clears_gate_and_revokes_other_sessions(self):
        current_token = self.login().json()['token']
        other_token = self.login().json()['token']
        current_session = InstructorSession.objects.order_by('id').first()
        other_session = InstructorSession.objects.order_by('id').last()

        response = self.post_json(
            '/datapipeline/api/instructor_password/',
            {
                'current_password': self.temporary_password,
                'new_password': 'ACompletelyNewPass123!',
            },
            token=current_token,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'password_changed'})
        self.account.refresh_from_db()
        self.user.refresh_from_db()
        current_session.refresh_from_db()
        other_session.refresh_from_db()
        self.assertFalse(self.account.must_change_password)
        self.assertTrue(self.user.check_password('ACompletelyNewPass123!'))
        self.assertIsNone(current_session.revoked_at)
        self.assertIsNotNone(other_session.revoked_at)

        current_me = self.client.get(
            '/datapipeline/api/instructor_me/',
            HTTP_AUTHORIZATION=f'Bearer {current_token}',
        )
        other_me = self.client.get(
            '/datapipeline/api/instructor_me/',
            HTTP_AUTHORIZATION=f'Bearer {other_token}',
        )
        self.assertEqual(current_me.status_code, 200)
        self.assertEqual(other_me.status_code, 401)

        event = InstructorAuditEvent.objects.get(
            action=InstructorAuditEvent.ACTION_PASSWORD_CHANGED,
        )
        self.assertEqual(event.actor, self.account)
        self.assertEqual(event.session, current_session)
        self.assertEqual(event.metadata, {})

    def test_password_change_rolls_back_when_audit_write_fails(self):
        token = self.login().json()['token']

        with patch(
            'datapipeline.instructor_views.record_instructor_event',
            side_effect=ValidationError('audit failed'),
        ):
            with self.assertRaises(ValidationError):
                self.post_json(
                    '/datapipeline/api/instructor_password/',
                    {
                        'current_password': self.temporary_password,
                        'new_password': 'ACompletelyNewPass123!',
                    },
                    token=token,
                )

        self.account.refresh_from_db()
        self.user.refresh_from_db()
        self.assertTrue(self.account.must_change_password)
        self.assertTrue(self.user.check_password(self.temporary_password))
        self.assertFalse(InstructorAuditEvent.objects.filter(
            action=InstructorAuditEvent.ACTION_PASSWORD_CHANGED,
        ).exists())

    def test_profile_patch_requires_authentication_and_password_readiness(self):
        missing = self.client.patch(
            '/datapipeline/api/instructor_me/',
            data=json.dumps({'display_name': 'Prof. Updated'}),
            content_type='application/json',
        )
        token = self.login().json()['token']
        gated = self.client.patch(
            '/datapipeline/api/instructor_me/',
            data=json.dumps({'display_name': 'Prof. Updated'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json(), {'error': 'authentication_required'})
        self.assertEqual(gated.status_code, 403)
        self.assertEqual(gated.json(), {'error': 'password_change_required'})
        self.account.refresh_from_db()
        self.assertEqual(self.account.display_name, 'Prof. Test')

    def test_profile_patch_rejects_unknown_blank_and_overlong_values(self):
        self.account.must_change_password = False
        self.account.save(update_fields=['must_change_password'])
        token = self.login().json()['token']
        invalid_payloads = [
            {'display_name': 'Prof. Updated', 'email': 'other@example.edu'},
            {'display_name': '   '},
            {'display_name': 'x' * 101},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.patch(
                    '/datapipeline/api/instructor_me/',
                    data=json.dumps(payload),
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Bearer {token}',
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {'error': 'invalid_profile'})

        self.account.refresh_from_db()
        self.assertEqual(self.account.display_name, 'Prof. Test')
        self.assertFalse(InstructorAuditEvent.objects.filter(
            action=InstructorAuditEvent.ACTION_PROFILE_UPDATED,
        ).exists())

    def test_profile_patch_trims_name_audits_and_preserves_course_snapshot(self):
        self.account.must_change_password = False
        self.account.save(update_fields=['must_change_password'])
        course = Course.objects.create(
            course_id='historical-course',
            course_name='Historical Course',
            instructor_name='Prof. Original',
            password=make_password(None),
        )
        token = self.login().json()['token']

        response = self.client.patch(
            '/datapipeline/api/instructor_me/',
            data=json.dumps({'display_name': '  Prof. Updated  '}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['display_name'], 'Prof. Updated')
        self.account.refresh_from_db()
        course.refresh_from_db()
        self.assertEqual(self.account.display_name, 'Prof. Updated')
        self.assertEqual(course.instructor_name, 'Prof. Original')
        event = InstructorAuditEvent.objects.get(
            action=InstructorAuditEvent.ACTION_PROFILE_UPDATED,
        )
        self.assertEqual(event.actor, self.account)
        self.assertEqual(event.metadata, {'changed_field': 'display_name'})

    def test_profile_patch_rolls_back_when_audit_write_fails(self):
        self.account.must_change_password = False
        self.account.save(update_fields=['must_change_password'])
        token = self.login().json()['token']

        with patch(
            'datapipeline.instructor_views.record_instructor_event',
            side_effect=ValidationError('audit failed'),
        ):
            with self.assertRaises(ValidationError):
                self.client.patch(
                    '/datapipeline/api/instructor_me/',
                    data=json.dumps({'display_name': 'Prof. Updated'}),
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Bearer {token}',
                )

        self.account.refresh_from_db()
        self.assertEqual(self.account.display_name, 'Prof. Test')
