import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
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
    LEAIChatMessage,
    LEAIChatSession,
    LEAIPdfIngestBatch,
    LEAIPdfIngestJob,
    LEAIQuickTake,
    TeamConfiguration,
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


class TeamAndPdfEndpointMatrixTests(CourseAndSurveyEndpointMatrixTests):
    test_managed_endpoints_reject_missing_and_wrong_course_tokens = None
    test_owner_can_use_all_endpoints_and_writes_exact_events = None
    test_legacy_course_endpoints_remain_compatible_without_actor = None
    test_course_and_survey_mutations_roll_back_when_audit_fails = None

    def setUp(self):
        super().setUp()
        self.team_update = TeamConfiguration.objects.create(
            course=self.course, name='Update Me',
        )
        self.team_archive = TeamConfiguration.objects.create(
            course=self.course, name='Archive Me',
        )
        self.team_delete = TeamConfiguration.objects.create(
            course=self.course, name='Delete Me',
        )

        self.start_survey = self._create_survey('pdf-start')
        self.detail_survey = self._create_survey('pdf-detail')
        self.commit_survey = self._create_survey('pdf-commit')
        self.revert_survey = self._create_survey('pdf-revert')
        self.detail_job = LEAIPdfIngestJob.objects.create(
            survey=self.detail_survey,
            status=LEAIPdfIngestJob.STATUS_READY,
            items=[],
        )
        self.commit_job = LEAIPdfIngestJob.objects.create(
            survey=self.commit_survey,
            status=LEAIPdfIngestJob.STATUS_READY,
            items=[],
        )
        self.revert_batch = LEAIPdfIngestBatch.objects.create(
            survey=self.revert_survey,
            student_count=1,
            message_count=1,
        )
        self.revert_message = FeedbackMessage.objects.create(
            session_id='pdf-revert-session',
            student_id='student-1',
            sent_by='student',
            content='PDF response',
            gpt_used=self.revert_survey.name,
            gpt_id=self.revert_survey.pk,
            source=FeedbackMessage.SOURCE_PDF,
            pdf_batch=self.revert_batch,
        )

    def _create_survey(self, public_id):
        return FeedbackGPT.objects.create(
            public_id=public_id,
            name=public_id,
            instructions='Reflect.',
            created_by='Prof. A',
            course=self.course,
            mode='general',
        )

    def team_pdf_cases(self):
        return [
            ('team_list', 'get', '/datapipeline/api/team_configurations/', {
                'course_id': self.course.course_id,
            }, 'query'),
            ('team_create', 'post', '/datapipeline/api/team_configurations/create/', {
                'course_id': self.course.course_id,
                'name': 'Created Configuration',
                'teams': [{'number': 1, 'size': 4}],
            }, 'json'),
            ('team_update', 'post', '/datapipeline/api/team_configurations/update/', {
                'id': self.team_update.pk,
                'name': 'Updated Configuration',
            }, 'json'),
            ('team_archive', 'post', '/datapipeline/api/team_configurations/archive/', {
                'id': self.team_archive.pk,
            }, 'json'),
            ('team_delete', 'post', '/datapipeline/api/team_configurations/delete/', {
                'id': self.team_delete.pk,
            }, 'json'),
            ('pdf_start', 'post', '/datapipeline/api/leai_pdf_ingest/start/', {
                'survey_id': self.start_survey.pk,
                'attributions': json.dumps({'reflection.pdf': 'student-1'}),
                'files': [SimpleUploadedFile(
                    'reflection.pdf', b'%PDF-test', content_type='application/pdf',
                )],
            }, 'multipart'),
            ('pdf_detail', 'get', (
                f'/datapipeline/api/leai_pdf_ingest/{self.detail_job.pk}/'
            ), {}, 'query'),
            ('pdf_abandon', 'delete', (
                f'/datapipeline/api/leai_pdf_ingest/{self.detail_job.pk}/'
            ), {}, 'query'),
            ('pdf_commit', 'post', (
                f'/datapipeline/api/leai_pdf_ingest/{self.commit_job.pk}/commit/'
            ), {
                'items': [{
                    'filename': 'reflection.pdf',
                    'student_id': 'student-1',
                    'mapping': {'__pdf_fulltext__': 'Reflection text'},
                    'skip': False,
                }],
                'dedup_decisions': {},
            }, 'json'),
            ('pdf_roster', 'get', '/datapipeline/api/leai_pdf_ingest/roster/', {
                'survey_id': self.survey.pk,
            }, 'query'),
            ('pdf_dedup', 'post', '/datapipeline/api/leai_pdf_ingest/dedup_check/', {
                'survey_id': self.survey.pk,
                'student_ids': ['student-1'],
            }, 'json'),
            ('pdf_batches', 'get', '/datapipeline/api/leai_pdf_ingest_batches/', {
                'survey_id': self.revert_survey.pk,
            }, 'query'),
            ('pdf_revert', 'post', (
                f'/datapipeline/api/leai_pdf_ingest_batches/{self.revert_batch.pk}/revert/'
            ), {}, 'json'),
        ]

    def call_team_pdf_endpoint(self, method, path, data, encoding, token=None):
        headers = {}
        if token:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        if method == 'get':
            return self.client.get(path, data=data, **headers)
        if method == 'delete':
            return self.client.delete(path, **headers)
        if encoding == 'multipart':
            return self.client.post(path, data=data, **headers)
        return self.client.post(
            path,
            data=json.dumps(data),
            content_type='application/json',
            **headers,
        )

    def test_team_and_pdf_methods_reject_missing_and_wrong_course_tokens(self):
        write_names = {
            'team_create', 'team_update', 'team_archive', 'team_delete',
            'pdf_start', 'pdf_abandon', 'pdf_commit', 'pdf_revert',
        }
        for name, method, path, data, encoding in self.team_pdf_cases():
            with self.subTest(name=name, token='missing'):
                response = self.call_team_pdf_endpoint(method, path, data, encoding)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {'error': 'authentication_required'})
            with self.subTest(name=name, token='wrong-course'):
                response = self.call_team_pdf_endpoint(
                    method, path, data, encoding, token=self.other_token,
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {'error': 'course_access_denied'})

        self.assertEqual(
            InstructorAuditEvent.objects.filter(
                action=InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
            ).count(),
            len(write_names),
        )
        self.assertTrue(TeamConfiguration.objects.filter(pk=self.team_delete.pk).exists())
        self.assertTrue(LEAIPdfIngestJob.objects.filter(pk=self.detail_job.pk).exists())
        self.assertTrue(LEAIPdfIngestJob.objects.filter(pk=self.commit_job.pk).exists())
        self.assertTrue(FeedbackMessage.objects.filter(pk=self.revert_message.pk).exists())

    def test_reads_need_membership_and_writes_need_publish_capability(self):
        membership = self.course.memberships.get()
        membership.role = CourseMembership.ROLE_INSTRUCTOR
        membership.can_publish = False
        membership.save(update_fields=['role', 'can_publish'])
        write_names = {
            'team_create', 'team_update', 'team_archive', 'team_delete',
            'pdf_start', 'pdf_abandon', 'pdf_commit', 'pdf_revert',
        }

        for name, method, path, data, encoding in self.team_pdf_cases():
            with self.subTest(name=name):
                response = self.call_team_pdf_endpoint(
                    method, path, data, encoding, token=self.token,
                )
                if name in write_names:
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json(), {'error': 'capability_denied'})
                else:
                    self.assertEqual(response.status_code, 200)

        denials = InstructorAuditEvent.objects.filter(
            action=InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
        )
        self.assertEqual(denials.count(), len(write_names))
        self.assertTrue(
            all(
                metadata == {'reason_code': 'capability_denied'}
                for metadata in denials.values_list('metadata', flat=True)
            ),
        )

    def test_legacy_team_configuration_flow_remains_compatible_without_actor(self):
        update_cfg = TeamConfiguration.objects.create(
            course=self.legacy_course, name='Legacy Update',
        )
        archive_cfg = TeamConfiguration.objects.create(
            course=self.legacy_course, name='Legacy Archive',
        )
        delete_cfg = TeamConfiguration.objects.create(
            course=self.legacy_course, name='Legacy Delete',
        )
        cases = [
            ('get', '/datapipeline/api/team_configurations/', {
                'course_id': self.legacy_course.course_id,
            }, 'query'),
            ('post', '/datapipeline/api/team_configurations/create/', {
                'course_id': self.legacy_course.course_id,
                'name': 'Legacy Create',
                'teams': [],
            }, 'json'),
            ('post', '/datapipeline/api/team_configurations/update/', {
                'id': update_cfg.pk,
                'name': 'Legacy Updated',
            }, 'json'),
            ('post', '/datapipeline/api/team_configurations/archive/', {
                'id': archive_cfg.pk,
            }, 'json'),
            ('post', '/datapipeline/api/team_configurations/delete/', {
                'id': delete_cfg.pk,
            }, 'json'),
        ]

        for method, path, data, encoding in cases:
            with self.subTest(path=path):
                response = self.call_team_pdf_endpoint(
                    method, path, data, encoding,
                )
                self.assertEqual(response.status_code, 200)

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_owner_flow_emits_exact_write_events_and_no_read_events(self):
        with patch('datapipeline.leai_pdf_ingest.threading.Thread.start'):
            responses = []
            for name, method, path, data, encoding in self.team_pdf_cases():
                with self.subTest(name=name):
                    response = self.call_team_pdf_endpoint(
                        method, path, data, encoding, token=self.token,
                    )
                    responses.append((name, response.status_code))

        for name, status in responses:
            with self.subTest(name=name, status=status):
                self.assertIn(status, {200, 201, 202, 204})

        expected_actions = {
            InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_CREATED,
            InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_UPDATED,
            InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_ARCHIVED,
            InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_DELETED,
            InstructorAuditEvent.ACTION_PDF_INGEST_STARTED,
            InstructorAuditEvent.ACTION_PDF_INGEST_ABANDONED,
            InstructorAuditEvent.ACTION_PDF_INGEST_COMMITTED,
            InstructorAuditEvent.ACTION_PDF_INGEST_REVERTED,
        }
        events = InstructorAuditEvent.objects.filter(
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        )
        self.assertEqual(events.count(), len(expected_actions))
        self.assertEqual(set(events.values_list('action', flat=True)), expected_actions)
        self.assertEqual(
            events.get(
                action=InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_UPDATED,
            ).metadata,
            {'changed_fields': ['name']},
        )
        self.assertEqual(
            events.get(action=InstructorAuditEvent.ACTION_PDF_INGEST_STARTED).metadata,
            {'file_count': 1},
        )
        self.assertEqual(
            events.get(action=InstructorAuditEvent.ACTION_PDF_INGEST_COMMITTED).metadata,
            {'student_count': 1, 'message_count': 1},
        )
        self.assertEqual(
            events.get(action=InstructorAuditEvent.ACTION_PDF_INGEST_REVERTED).metadata,
            {'deleted_count': 1},
        )

    def test_team_and_pdf_mutations_roll_back_when_audit_fails(self):
        mutation_names = {
            'team_create', 'team_update', 'team_archive', 'team_delete',
            'pdf_start', 'pdf_abandon', 'pdf_commit', 'pdf_revert',
        }
        for name, method, path, data, encoding in self.team_pdf_cases():
            if name not in mutation_names:
                continue
            before = {
                'team_count': TeamConfiguration.objects.count(),
                'update_name': TeamConfiguration.objects.get(pk=self.team_update.pk).name,
                'archive_state': TeamConfiguration.objects.get(pk=self.team_archive.pk).archived,
                'delete_exists': TeamConfiguration.objects.filter(pk=self.team_delete.pk).exists(),
                'job_count': LEAIPdfIngestJob.objects.count(),
                'detail_exists': LEAIPdfIngestJob.objects.filter(pk=self.detail_job.pk).exists(),
                'commit_exists': LEAIPdfIngestJob.objects.filter(pk=self.commit_job.pk).exists(),
                'batch_count': LEAIPdfIngestBatch.objects.count(),
                'revert_message_exists': FeedbackMessage.objects.filter(
                    pk=self.revert_message.pk,
                ).exists(),
                'reverted_at': LEAIPdfIngestBatch.objects.get(
                    pk=self.revert_batch.pk,
                ).reverted_at,
            }
            with self.subTest(name=name):
                with patch(
                    'datapipeline.views.record_instructor_event',
                    side_effect=ValidationError('audit failed'),
                ), patch('datapipeline.leai_pdf_ingest.threading.Thread.start'):
                    self.client.raise_request_exception = False
                    response = self.call_team_pdf_endpoint(
                        method, path, data, encoding, token=self.token,
                    )
                    self.client.raise_request_exception = True
                self.assertIn(response.status_code, {400, 500})
                self.assertEqual(TeamConfiguration.objects.count(), before['team_count'])
                self.assertEqual(
                    TeamConfiguration.objects.get(pk=self.team_update.pk).name,
                    before['update_name'],
                )
                self.assertEqual(
                    TeamConfiguration.objects.get(pk=self.team_archive.pk).archived,
                    before['archive_state'],
                )
                self.assertEqual(
                    TeamConfiguration.objects.filter(pk=self.team_delete.pk).exists(),
                    before['delete_exists'],
                )
                self.assertEqual(LEAIPdfIngestJob.objects.count(), before['job_count'])
                self.assertEqual(
                    LEAIPdfIngestJob.objects.filter(pk=self.detail_job.pk).exists(),
                    before['detail_exists'],
                )
                self.assertEqual(
                    LEAIPdfIngestJob.objects.filter(pk=self.commit_job.pk).exists(),
                    before['commit_exists'],
                )
                self.assertEqual(LEAIPdfIngestBatch.objects.count(), before['batch_count'])
                self.assertEqual(
                    FeedbackMessage.objects.filter(pk=self.revert_message.pk).exists(),
                    before['revert_message_exists'],
                )
                self.assertEqual(
                    LEAIPdfIngestBatch.objects.get(pk=self.revert_batch.pk).reverted_at,
                    before['reverted_at'],
                )

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_failed_pdf_commit_and_revert_do_not_emit_success_events(self):
        self.commit_job.status = LEAIPdfIngestJob.STATUS_FAILED
        self.commit_job.save(update_fields=['status'])
        self.revert_batch.reverted_at = timezone.now()
        self.revert_batch.save(update_fields=['reverted_at'])

        commit = self.call_team_pdf_endpoint(
            'post',
            f'/datapipeline/api/leai_pdf_ingest/{self.commit_job.pk}/commit/',
            {'items': [], 'dedup_decisions': {}},
            'json',
            token=self.token,
        )
        revert = self.call_team_pdf_endpoint(
            'post',
            f'/datapipeline/api/leai_pdf_ingest_batches/{self.revert_batch.pk}/revert/',
            {},
            'json',
            token=self.token,
        )

        self.assertEqual(commit.status_code, 400)
        self.assertEqual(revert.status_code, 409)
        self.assertFalse(InstructorAuditEvent.objects.filter(
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        ).exists())

class AnalysisEndpointMatrixTests(CourseAndSurveyEndpointMatrixTests):
    test_managed_endpoints_reject_missing_and_wrong_course_tokens = None
    test_owner_can_use_all_endpoints_and_writes_exact_events = None
    test_legacy_course_endpoints_remain_compatible_without_actor = None
    test_course_and_survey_mutations_roll_back_when_audit_fails = None

    def setUp(self):
        super().setUp()
        for index in range(1, 5):
            FeedbackMessage.objects.create(
                session_id=f'student-session-{index}',
                student_id='anonymous',
                sent_by='user-message',
                content=f'Student response {index}',
                gpt_used=self.survey.name,
                gpt_id=self.survey.pk,
            )
        self.chat_session = LEAIChatSession.objects.create(
            course=self.course,
            title='Existing analysis',
        )
        self.chat_message = LEAIChatMessage.objects.create(
            session=self.chat_session,
            role='assistant',
            text='Existing answer',
        )
        self.quicktake = LEAIQuickTake.objects.create(
            course=self.course,
            scope_key='course',
            bullets=[],
            verification=[],
            system_prompt='',
            user_text='',
            model_name='',
            status=LEAIQuickTake.STATUS_READY,
        )

        self.legacy_chat_session = LEAIChatSession.objects.create(
            course=self.legacy_course,
            title='Legacy analysis',
        )
        self.legacy_chat_message = LEAIChatMessage.objects.create(
            session=self.legacy_chat_session,
            role='assistant',
            text='Legacy answer',
        )
        self.legacy_quicktake = LEAIQuickTake.objects.create(
            course=self.legacy_course,
            scope_key='course',
            bullets=[],
            verification=[],
            system_prompt='',
            user_text='',
            model_name='',
            status=LEAIQuickTake.STATUS_READY,
        )
        for index in range(5):
            FeedbackMessage.objects.create(
                session_id=f'legacy-student-{index}',
                student_id='anonymous',
                sent_by='user-message',
                content=f'Legacy response {index}',
                gpt_used=self.legacy_survey.name,
                gpt_id=self.legacy_survey.pk,
            )

    def analysis_cases(self, *, course=None, chat_session=None,
                       chat_message=None, quicktake=None):
        course = course or self.course
        chat_session = chat_session or self.chat_session
        chat_message = chat_message or self.chat_message
        quicktake = quicktake or self.quicktake
        return [
            ('session_list', 'get', '/datapipeline/api/leai_chat_sessions/', {
                'course_id': course.course_id,
            }),
            ('session_create', 'post', '/datapipeline/api/leai_chat_sessions/', {
                'course_id': course.course_id,
                'title': 'New analysis',
                'scope': {'kind': 'course'},
            }),
            ('session_detail', 'get', (
                f'/datapipeline/api/leai_chat_sessions/{chat_session.pk}/'
            ), {}),
            ('session_update', 'patch', (
                f'/datapipeline/api/leai_chat_sessions/{chat_session.pk}/'
            ), {'title': 'Renamed analysis'}),
            ('session_delete', 'delete', (
                f'/datapipeline/api/leai_chat_sessions/{chat_session.pk}/'
            ), {}),
            ('turn', 'post', (
                f'/datapipeline/api/leai_chat_sessions/{chat_session.pk}/turn/'
            ), {'user_text': 'What themes stand out?'}),
            ('message', 'get', (
                f'/datapipeline/api/leai_chat_sessions/{chat_session.pk}/'
                f'messages/{chat_message.pk}/'
            ), {}),
            ('quicktake_get', 'get', '/datapipeline/api/leai_quicktake/', {
                'course_id': course.course_id,
                'scope_key': quicktake.scope_key,
            }),
            ('quicktake_delete', 'delete', '/datapipeline/api/leai_quicktake/', {
                'course_id': course.course_id,
                'scope_key': quicktake.scope_key,
            }),
            ('quicktake_generate', 'post', (
                '/datapipeline/api/leai_quicktake/generate/'
            ), {
                'course_id': course.course_id,
                'scope_key': quicktake.scope_key,
                'scope': {'kind': 'course'},
            }),
        ]

    def call_endpoint(self, method, path, data, token=None):
        headers = {}
        if token:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        if method == 'get':
            return self.client.get(path, data=data, **headers)
        if method == 'delete':
            if data:
                path = f'{path}?{urlencode(data)}'
            return self.client.delete(path, **headers)
        if method == 'patch':
            return self.client.patch(
                path,
                data=json.dumps(data),
                content_type='application/json',
                **headers,
            )
        return self.client.post(
            path,
            data=json.dumps(data),
            content_type='application/json',
            **headers,
        )

    def test_analysis_methods_reject_missing_and_wrong_course_tokens(self):
        for name, method, path, data in self.analysis_cases():
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
            6,
        )

    def test_owner_analysis_flow_emits_write_events_and_no_read_events(self):
        with patch('datapipeline.leai_analysis.threading.Thread.start'):
            listed = self.call_endpoint(
                'get',
                '/datapipeline/api/leai_chat_sessions/',
                {'course_id': self.course.course_id},
                token=self.token,
            )
            created = self.call_endpoint(
                'post',
                '/datapipeline/api/leai_chat_sessions/',
                {
                    'course_id': self.course.course_id,
                    'title': 'New analysis',
                    'scope': {'kind': 'course'},
                },
                token=self.token,
            )
            detail = self.call_endpoint(
                'get',
                f'/datapipeline/api/leai_chat_sessions/{self.chat_session.pk}/',
                {},
                token=self.token,
            )
            updated = self.call_endpoint(
                'patch',
                f'/datapipeline/api/leai_chat_sessions/{self.chat_session.pk}/',
                {'title': 'Renamed analysis'},
                token=self.token,
            )
            message = self.call_endpoint(
                'get',
                (
                    f'/datapipeline/api/leai_chat_sessions/{self.chat_session.pk}/'
                    f'messages/{self.chat_message.pk}/'
                ),
                {},
                token=self.token,
            )
            turn = self.call_endpoint(
                'post',
                f'/datapipeline/api/leai_chat_sessions/{self.chat_session.pk}/turn/',
                {'user_text': 'What themes stand out?'},
                token=self.token,
            )
            quicktake_get = self.call_endpoint(
                'get',
                '/datapipeline/api/leai_quicktake/',
                {'course_id': self.course.course_id, 'scope_key': 'course'},
                token=self.token,
            )
            quicktake_generate = self.call_endpoint(
                'post',
                '/datapipeline/api/leai_quicktake/generate/',
                {
                    'course_id': self.course.course_id,
                    'scope_key': 'course',
                    'scope': {'kind': 'course'},
                },
                token=self.token,
            )
            quicktake_delete = self.call_endpoint(
                'delete',
                '/datapipeline/api/leai_quicktake/',
                {'course_id': self.course.course_id, 'scope_key': 'course'},
                token=self.token,
            )
            session_delete = self.call_endpoint(
                'delete',
                f'/datapipeline/api/leai_chat_sessions/{self.chat_session.pk}/',
                {},
                token=self.token,
            )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(message.status_code, 200)
        self.assertEqual(turn.status_code, 202)
        self.assertEqual(quicktake_get.status_code, 200)
        self.assertEqual(quicktake_generate.status_code, 202)
        self.assertEqual(quicktake_delete.status_code, 204)
        self.assertEqual(session_delete.status_code, 204)

        expected_actions = {
            InstructorAuditEvent.ACTION_ANALYSIS_SESSION_CREATED,
            InstructorAuditEvent.ACTION_ANALYSIS_SESSION_UPDATED,
            InstructorAuditEvent.ACTION_ANALYSIS_SESSION_DELETED,
            InstructorAuditEvent.ACTION_ANALYSIS_TURN_STARTED,
            InstructorAuditEvent.ACTION_ANALYSIS_QUICKTAKE_GENERATED,
            InstructorAuditEvent.ACTION_ANALYSIS_QUICKTAKE_DELETED,
        }
        events = InstructorAuditEvent.objects.filter(
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        )
        self.assertEqual(events.count(), len(expected_actions))
        self.assertEqual(set(events.values_list('action', flat=True)), expected_actions)
        self.assertEqual(
            events.get(
                action=InstructorAuditEvent.ACTION_ANALYSIS_SESSION_UPDATED,
            ).metadata,
            {'changed_fields': ['title']},
        )
        self.assertEqual(
            events.get(
                action=(
                    InstructorAuditEvent.ACTION_ANALYSIS_QUICKTAKE_GENERATED
                ),
            ).metadata,
            {'scope_kind': 'course'},
        )

    def test_legacy_analysis_methods_remain_compatible_without_actor(self):
        cases = self.analysis_cases(
            course=self.legacy_course,
            chat_session=self.legacy_chat_session,
            chat_message=self.legacy_chat_message,
            quicktake=self.legacy_quicktake,
        )
        destructive = {'session_delete', 'quicktake_delete'}
        ordered = [case for case in cases if case[0] not in destructive]
        ordered += [case for case in cases if case[0] in destructive]

        with patch('datapipeline.leai_analysis.threading.Thread.start'):
            for name, method, path, data in ordered:
                with self.subTest(name=name):
                    response = self.call_endpoint(method, path, data)
                    self.assertIn(response.status_code, {200, 201, 202, 204})

        self.assertFalse(InstructorAuditEvent.objects.exists())

    def test_analysis_mutations_roll_back_when_audit_fails(self):
        cases = [
            ('session_create', 'post', '/datapipeline/api/leai_chat_sessions/', {
                'course_id': self.course.course_id,
                'title': 'Should roll back',
                'scope': {'kind': 'course'},
            }),
            ('session_update', 'patch', (
                f'/datapipeline/api/leai_chat_sessions/{self.chat_session.pk}/'
            ), {'title': 'Should roll back'}),
            ('turn', 'post', (
                f'/datapipeline/api/leai_chat_sessions/{self.chat_session.pk}/turn/'
            ), {'user_text': 'Should roll back'}),
            ('quicktake_generate', 'post', (
                '/datapipeline/api/leai_quicktake/generate/'
            ), {
                'course_id': self.course.course_id,
                'scope_key': 'course',
                'scope': {'kind': 'course'},
            }),
            ('quicktake_delete', 'delete', '/datapipeline/api/leai_quicktake/', {
                'course_id': self.course.course_id,
                'scope_key': 'course',
            }),
            ('session_delete', 'delete', (
                f'/datapipeline/api/leai_chat_sessions/{self.chat_session.pk}/'
            ), {}),
        ]

        for name, method, path, data in cases:
            before_sessions = LEAIChatSession.objects.count()
            before_messages = LEAIChatMessage.objects.count()
            before_quicktakes = LEAIQuickTake.objects.count()
            before_quicktake_status = LEAIQuickTake.objects.get(
                pk=self.quicktake.pk,
            ).status
            before_title = LEAIChatSession.objects.get(
                pk=self.chat_session.pk,
            ).title
            with self.subTest(name=name):
                with patch(
                    'datapipeline.views.record_instructor_event',
                    side_effect=ValidationError('audit failed'),
                ), patch('datapipeline.leai_analysis.threading.Thread.start'):
                    self.client.raise_request_exception = False
                    response = self.call_endpoint(
                        method,
                        path,
                        data,
                        token=self.token,
                    )
                    self.client.raise_request_exception = True
                self.assertIn(response.status_code, {400, 500})
                self.assertEqual(LEAIChatSession.objects.count(), before_sessions)
                self.assertEqual(LEAIChatMessage.objects.count(), before_messages)
                self.assertEqual(LEAIQuickTake.objects.count(), before_quicktakes)
                self.assertEqual(
                    LEAIQuickTake.objects.get(pk=self.quicktake.pk).status,
                    before_quicktake_status,
                )
                self.assertEqual(
                    LEAIChatSession.objects.get(pk=self.chat_session.pk).title,
                    before_title,
                )

        self.assertFalse(InstructorAuditEvent.objects.exists())
