import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from datapipeline.instructor_auth import (
    authorize_instructor_course,
    issue_instructor_session,
    membership_allows,
)
from datapipeline.models import (
    Course,
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
    InstructorSession,
)


class InstructorCourseAuthorizationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.institution = Institution.objects.create(
            slug='ucsc',
            name='UC Santa Cruz',
        )
        self.course = Course.objects.create(
            course_id='owned-course',
            course_name='Owned Course',
            instructor_name='Prof. Owner',
            password='unusable',
            institution=self.institution,
            legacy_password_login_enabled=False,
        )
        self.other_course = Course.objects.create(
            course_id='other-course',
            course_name='Other Course',
            instructor_name='Prof. Other',
            password='unusable',
            institution=self.institution,
            legacy_password_login_enabled=False,
        )
        self.other_institution = Institution.objects.create(
            slug='other-university',
            name='Other University',
        )
        self.cross_institution_course = Course.objects.create(
            course_id='cross-institution-course',
            course_name='Cross Institution Course',
            instructor_name='Prof. Other',
            password='unusable',
            institution=self.other_institution,
            legacy_password_login_enabled=False,
        )
        self.user = get_user_model().objects.create_user(
            username='owner@example.edu',
            email='owner@example.edu',
            password='TemporaryPass123!',
        )
        self.account = InstructorAccount.objects.create(
            user=self.user,
            email=self.user.email,
            display_name='Prof. Owner',
            must_change_password=False,
        )
        self.institution_membership = InstitutionMembership.objects.create(
            institution=self.institution,
            instructor=self.account,
        )
        self.membership = CourseMembership.objects.create(
            course=self.course,
            institution_membership=self.institution_membership,
            role=CourseMembership.ROLE_OWNER,
        )
        self.raw_token, self.session = issue_instructor_session(self.account)

    def request(self, token=None, authorization=None):
        request = self.factory.post('/instructor-operation/')
        if authorization is not None:
            request.META['HTTP_AUTHORIZATION'] = authorization
        elif token is not None:
            request.META['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        return request

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertEqual(json.loads(response.content), {'error': code})
        self.assertEqual(response['Cache-Control'], 'no-store, private')

    def test_missing_malformed_and_expired_tokens_require_authentication(self):
        expired_token, expired_session = issue_instructor_session(self.account)
        expired_session.expires_at = timezone.now() - timedelta(seconds=1)
        expired_session.save(update_fields=['expires_at'])
        requests = [
            self.request(),
            self.request(authorization='Token malformed'),
            self.request(token=expired_token),
        ]

        for request in requests:
            with self.subTest(authorization=request.headers.get('Authorization')):
                account, session, membership, error = authorize_instructor_course(
                    request,
                    self.course,
                )
                self.assertIsNone(account)
                self.assertIsNone(session)
                self.assertIsNone(membership)
                self.assert_error(error, 401, 'authentication_required')

    def test_inactive_membership_is_denied_for_exact_course(self):
        self.membership.is_active = False
        self.membership.save(update_fields=['is_active'])

        account, session, membership, error = authorize_instructor_course(
            self.request(token=self.raw_token),
            self.course,
        )

        self.assertEqual(account, self.account)
        self.assertEqual(session, self.session)
        self.assertIsNone(membership)
        self.assert_error(error, 403, 'course_access_denied')

    def test_required_password_change_blocks_course_authorization(self):
        self.account.must_change_password = True
        self.account.save(update_fields=['must_change_password'])

        account, session, membership, error = authorize_instructor_course(
            self.request(token=self.raw_token),
            self.course,
        )

        self.assertEqual(account, self.account)
        self.assertEqual(session, self.session)
        self.assertIsNone(membership)
        self.assert_error(error, 403, 'password_change_required')

    def test_inactive_institution_membership_is_denied(self):
        self.institution_membership.is_active = False
        self.institution_membership.save(update_fields=['is_active'])

        _account, _session, membership, error = authorize_instructor_course(
            self.request(token=self.raw_token),
            self.course,
        )

        self.assertIsNone(membership)
        self.assert_error(error, 403, 'course_access_denied')

    def test_wrong_course_is_denied_without_accepting_another_course_id(self):
        account, session, membership, error = authorize_instructor_course(
            self.request(token=self.raw_token),
            self.other_course,
        )

        self.assertEqual(account, self.account)
        self.assertEqual(session, self.session)
        self.assertIsNone(membership)
        self.assert_error(error, 403, 'course_access_denied')

    def test_corrupt_cross_institution_membership_fails_closed(self):
        CourseMembership.objects.filter(pk=self.membership.pk).update(
            course=self.cross_institution_course,
        )

        _account, _session, membership, error = authorize_instructor_course(
            self.request(token=self.raw_token),
            self.cross_institution_course,
        )

        self.assertIsNone(membership)
        self.assert_error(error, 403, 'course_access_denied')

    def test_owner_bypasses_explicit_capability_flags(self):
        self.assertFalse(self.membership.can_publish)
        self.assertFalse(self.membership.can_export)

        for capability in (None, 'publish', 'export'):
            with self.subTest(capability=capability):
                account, session, membership, error = authorize_instructor_course(
                    self.request(token=self.raw_token),
                    self.course,
                    capability=capability,
                )
                self.assertEqual(account, self.account)
                self.assertEqual(session, self.session)
                self.assertEqual(membership, self.membership)
                self.assertIsNone(error)

    def test_instructor_requires_the_explicit_capability(self):
        self.membership.role = CourseMembership.ROLE_INSTRUCTOR
        self.membership.can_publish = True
        self.membership.can_export = False
        self.membership.save(update_fields=['role', 'can_publish', 'can_export'])

        allowed = authorize_instructor_course(
            self.request(token=self.raw_token),
            self.course,
            capability='publish',
        )
        denied = authorize_instructor_course(
            self.request(token=self.raw_token),
            self.course,
            capability='export',
        )

        self.assertEqual(allowed[2], self.membership)
        self.assertIsNone(allowed[3])
        self.assertEqual(denied[:3], (self.account, self.session, self.membership))
        self.assert_error(denied[3], 403, 'capability_denied')

    def test_ta_without_capability_is_denied(self):
        self.membership.role = CourseMembership.ROLE_TA
        self.membership.save(update_fields=['role'])

        _account, _session, membership, error = authorize_instructor_course(
            self.request(token=self.raw_token),
            self.course,
            capability='publish',
        )

        self.assertEqual(membership, self.membership)
        self.assert_error(error, 403, 'capability_denied')

    def test_membership_allows_fails_closed_for_unknown_capability(self):
        self.assertTrue(membership_allows(self.membership, None))
        self.assertTrue(membership_allows(self.membership, 'publish'))
        self.assertFalse(membership_allows(self.membership, 'unknown'))

    def test_revoked_token_requires_authentication(self):
        self.session.revoked_at = timezone.now()
        self.session.save(update_fields=['revoked_at'])

        account, session, membership, error = authorize_instructor_course(
            self.request(token=self.raw_token),
            self.course,
        )

        self.assertEqual((account, session, membership), (None, None, None))
        self.assert_error(error, 401, 'authentication_required')

    def test_session_fixture_is_the_only_active_token_used(self):
        self.assertTrue(InstructorSession.objects.filter(pk=self.session.pk).exists())
