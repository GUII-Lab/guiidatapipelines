import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory, TestCase
from django.utils import timezone

from datapipeline.instructor_auth import (
    authorize_instructor_course,
    issue_instructor_session,
    membership_allows,
)
from datapipeline.models import (
    Course,
    CourseMembership,
    FeedbackGPT,
    FeedbackMessage,
    Institution,
    InstitutionMembership,
    InstructorAccount,
    InstructorAuditEvent,
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


class CourseAndSurveyEndpointMatrixTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.institution = Institution.objects.create(
            slug='ucsc',
            name='UC Santa Cruz',
        )
        self.course = self.create_owned_course(
            'course-a',
            'owner-a@example.edu',
            self.institution,
        )
        self.other_course = self.create_owned_course(
            'course-b',
            'owner-b@example.edu',
            self.institution,
        )
        self.owner = self.course.memberships.get().institution_membership.instructor
        self.other_owner = (
            self.other_course.memberships.get().institution_membership.instructor
        )
        self.token, self.session = issue_instructor_session(self.owner)
        self.other_token, _other_session = issue_instructor_session(self.other_owner)
        self.survey = FeedbackGPT.objects.create(
            public_id='survey-a',
            name='Week 1',
            instructions='Reflect.',
            created_by='Prof. A',
            course=self.course,
            week_number=1,
            survey_label='Week 1',
            mode='general',
        )
        FeedbackMessage.objects.create(
            session_id='student-session',
            student_id='anonymous',
            sent_by='user-message',
            content='Student response',
            gpt_used=self.survey.name,
            gpt_id=self.survey.pk,
        )

        self.legacy_course = Course.objects.create(
            course_id='legacy-course',
            course_name='Legacy Course',
            instructor_name='Legacy Instructor',
            password='legacy',
            legacy_password_login_enabled=True,
        )
        self.legacy_survey = FeedbackGPT.objects.create(
            public_id='legacy-survey',
            name='Legacy Week',
            instructions='Reflect.',
            created_by='Legacy Instructor',
            course=self.legacy_course,
            week_number=1,
            survey_label='Legacy Week',
            mode='general',
        )

    def create_owned_course(self, course_id, email, institution):
        user = get_user_model().objects.create_user(
            username=email,
            email=email,
            password='TemporaryPass123!',
        )
        account = InstructorAccount.objects.create(
            user=user,
            email=email,
            display_name=email.split('@')[0],
            must_change_password=False,
        )
        institution_membership = InstitutionMembership.objects.create(
            institution=institution,
            instructor=account,
        )
        course = Course.objects.create(
            course_id=course_id,
            course_name=course_id,
            instructor_name=account.display_name,
            password='unusable',
            institution=institution,
            legacy_password_login_enabled=False,
        )
        CourseMembership.objects.create(
            course=course,
            institution_membership=institution_membership,
            role=CourseMembership.ROLE_OWNER,
        )
        return course

    def endpoint_cases(self, *, course=None, survey=None):
        course = course or self.course
        survey = survey or self.survey
        return [
            ('banner', 'post', '/datapipeline/api/update_course_banner/', {
                'course_id': course.course_id,
                'enabled': True,
            }),
            ('customization', 'post', '/datapipeline/api/update_course_customization/', {
                'course_id': course.course_id,
                'bot_display_name': 'Course Bot',
            }),
            ('create_survey', 'post', '/datapipeline/api/create_feedback_gpt/', {
                'course_id': course.course_id,
                'name': 'New Survey',
                'instructions': 'Reflect.',
                'instructor_name': 'Prof. Test',
                'mode': 'general',
            }),
            ('status', 'post', '/datapipeline/api/set_survey_status/', {
                'survey_id': survey.pk,
                'action': 'close',
            }),
            ('update_survey', 'post', '/datapipeline/api/update_survey/', {
                'survey_id': survey.pk,
                'survey_label': 'Updated Week',
            }),
            ('clone', 'post', '/datapipeline/api/clone_survey/', {
                'survey_id': survey.pk,
            }),
            ('delete', 'post', '/datapipeline/api/delete_survey/', {
                'survey_id': survey.pk,
            }),
            ('export', 'get', '/datapipeline/api/export_survey_responses/', {
                'survey_id': survey.pk,
            }),
            ('survey_list', 'get', '/datapipeline/api/feedback_gpts_by_course/', {
                'course_id': course.course_id,
            }),
            ('responses_by_survey', 'get', '/datapipeline/api/feedback_messages_by_gpt/', {
                'gpt_id': survey.pk,
            }),
            ('responses_by_course', 'get', '/datapipeline/api/feedback_messages_by_course/', {
                'course_id': course.course_id,
            }),
        ]

    def call_endpoint(self, method, path, data, token=None):
        headers = {}
        if token:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        if method == 'get':
            return self.client.get(path, data=data, **headers)
        return self.client.post(
            path,
            data=json.dumps(data),
            content_type='application/json',
            **headers,
        )

    def test_managed_endpoints_reject_missing_and_wrong_course_tokens(self):
        for name, method, path, data in self.endpoint_cases():
            with self.subTest(name=name, token='missing'):
                response = self.call_endpoint(method, path, data)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {
                    'error': 'authentication_required',
                })
            with self.subTest(name=name, token='wrong-course'):
                response = self.call_endpoint(
                    method,
                    path,
                    data,
                    token=self.other_token,
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {
                    'error': 'course_access_denied',
                })

        self.assertEqual(
            InstructorAuditEvent.objects.filter(
                action=InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
            ).count(),
            8,
        )
        self.assertFalse(InstructorAuditEvent.objects.exclude(
            action=InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
        ).exists())

    def test_owner_can_use_all_endpoints_and_writes_exact_events(self):
        cases = self.endpoint_cases()
        delete_case = next(case for case in cases if case[0] == 'delete')
        ordered_cases = [case for case in cases if case[0] != 'delete'] + [delete_case]

        for name, method, path, data in ordered_cases:
            with self.subTest(name=name):
                response = self.call_endpoint(method, path, data, token=self.token)
                self.assertIn(response.status_code, {200, 201})

        expected_actions = {
            InstructorAuditEvent.ACTION_COURSE_BANNER_UPDATED,
            InstructorAuditEvent.ACTION_COURSE_CUSTOMIZATION_UPDATED,
            InstructorAuditEvent.ACTION_SURVEY_CREATED,
            InstructorAuditEvent.ACTION_SURVEY_STATUS_CHANGED,
            InstructorAuditEvent.ACTION_SURVEY_UPDATED,
            InstructorAuditEvent.ACTION_SURVEY_CLONED,
            InstructorAuditEvent.ACTION_SURVEY_DELETED,
            InstructorAuditEvent.ACTION_SURVEY_RESPONSES_EXPORTED,
        }
        success_events = InstructorAuditEvent.objects.filter(
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        )
        self.assertEqual(success_events.count(), len(expected_actions))
        self.assertEqual(
            set(success_events.values_list('action', flat=True)),
            expected_actions,
        )
        export_event = success_events.get(
            action=InstructorAuditEvent.ACTION_SURVEY_RESPONSES_EXPORTED,
        )
        self.assertEqual(export_event.metadata, {'row_count': 1})
        self.assertFalse(InstructorAuditEvent.objects.filter(
            action=InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
        ).exists())

    def test_legacy_course_endpoints_remain_compatible_without_actor(self):
        cases = self.endpoint_cases(
            course=self.legacy_course,
            survey=self.legacy_survey,
        )
        delete_case = next(case for case in cases if case[0] == 'delete')
        ordered_cases = [case for case in cases if case[0] != 'delete'] + [delete_case]

        for name, method, path, data in ordered_cases:
            with self.subTest(name=name):
                response = self.call_endpoint(method, path, data)
                self.assertIn(response.status_code, {200, 201})

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_course_and_survey_mutations_roll_back_when_audit_fails(self):
        mutation_names = {
            'banner',
            'customization',
            'create_survey',
            'status',
            'update_survey',
            'clone',
            'delete',
        }
        original_survey_count = FeedbackGPT.objects.count()

        for name, method, path, data in self.endpoint_cases():
            if name not in mutation_names:
                continue
            before = {
                'banner_enabled': Course.objects.get(pk=self.course.pk).banner_enabled,
                'bot_display_name': Course.objects.get(pk=self.course.pk).bot_display_name,
                'survey_count': FeedbackGPT.objects.count(),
                'survey_exists': FeedbackGPT.objects.filter(pk=self.survey.pk).exists(),
                'survey_closed': FeedbackGPT.objects.get(pk=self.survey.pk).is_closed,
                'survey_label': FeedbackGPT.objects.get(pk=self.survey.pk).survey_label,
            }
            with self.subTest(name=name):
                with patch(
                    'datapipeline.views.record_instructor_event',
                    side_effect=ValidationError('audit failed'),
                ):
                    self.client.raise_request_exception = False
                    response = self.call_endpoint(
                        method,
                        path,
                        data,
                        token=self.token,
                    )
                    self.client.raise_request_exception = True
                self.assertIn(response.status_code, {400, 500})
                self.course.refresh_from_db()
                self.survey.refresh_from_db()
                self.assertEqual(self.course.banner_enabled, before['banner_enabled'])
                self.assertEqual(self.course.bot_display_name, before['bot_display_name'])
                self.assertEqual(FeedbackGPT.objects.count(), before['survey_count'])
                self.assertEqual(
                    FeedbackGPT.objects.filter(pk=self.survey.pk).exists(),
                    before['survey_exists'],
                )
                self.assertEqual(self.survey.is_closed, before['survey_closed'])
                self.assertEqual(self.survey.survey_label, before['survey_label'])

        self.assertEqual(FeedbackGPT.objects.count(), original_survey_count)
        self.assertFalse(InstructorAuditEvent.objects.exists())
