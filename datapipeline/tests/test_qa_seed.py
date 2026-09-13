import copy
import io
import json
import os
import secrets
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import connection
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from datapipeline.models import (
    Course,
    CourseMembership,
    FeedbackGPT,
    FeedbackMessage,
    Institution,
    InstitutionMembership,
    InstructorAccount,
    InstructorAuditEvent,
    LEAIChatMessage,
    LEAIChatSession,
    PreviewMessage,
    PreviewSession,
    QuestionSet,
    QuestionSetDraft,
    QuestionSetRevision,
    QuestionSetSurvey,
    ResponseSession,
)
from datapipeline.qa_seed import (
    EXPECTED_COUNTS,
    QA_ANALYSIS_SESSION_ID,
    QA_AUDIT_EVENT_IDS,
    QA_COURSE_IDS,
    QA_DRAFT_PUBLIC_ID,
    QA_INSTITUTION_SLUG,
    QA_INSTRUCTOR_EMAILS,
    QA_PREVIEW_PUBLIC_ID,
    QA_QUESTION_SET_PUBLIC_ID,
    QA_RESPONSE_PUBLIC_ID,
    QA_REVISION_PUBLIC_ID,
    QA_SEED_VERSION,
    QA_SURVEY_PUBLIC_ID,
    QASeedError,
    seed_qa_data,
)
from datapipeline.instructor_audit import record_instructor_event
from datapipeline.question_sets import create_draft, freeze_draft, save_draft
from datapipeline.response_sessions import persist_feedback_messages


EXPECTED_RESULT = {
    'seed_version': '2026-09-12.1',
    'institutions': 1,
    'instructor_accounts': 2,
    'auth_users': 2,
    'institution_memberships': 2,
    'courses': 3,
    'course_memberships': 4,
    'question_sets': 1,
    'question_set_drafts': 1,
    'question_set_revisions': 1,
    'question_set_validation_runs': 2,
    'preview_sessions': 1,
    'preview_messages': 11,
    'feedback_gpts': 1,
    'question_set_surveys': 1,
    'response_sessions': 1,
    'feedback_messages': 11,
    'analysis_sessions': 1,
    'analysis_messages': 2,
    'instructor_audit_events': 9,
}


def _runtime_password():
    return f'{secrets.token_urlsafe(28)}aA1!'


@override_settings(LEAI_ENV='qa')
class QASeedTests(TestCase):
    def setUp(self):
        self.passwords = {
            email: _runtime_password()
            for email in QA_INSTRUCTOR_EMAILS
        }

    def seed(self, *, reset=False, confirm=None, passwords=True):
        return seed_qa_data(
            reset=reset,
            confirm=confirm,
            instructor_passwords=self.passwords if passwords else None,
        )

    def test_seed_is_idempotent_and_reports_exact_persisted_counts(self):
        first = self.seed()
        password_hashes = dict(
            InstructorAccount.objects.filter(email__in=QA_INSTRUCTOR_EMAILS)
            .values_list('email', 'user__password')
        )

        second = self.seed(passwords=False)

        self.assertEqual(first, EXPECTED_RESULT)
        self.assertEqual(second, EXPECTED_RESULT)
        self.assertEqual(EXPECTED_COUNTS, {
            key: value
            for key, value in EXPECTED_RESULT.items()
            if key != 'seed_version'
        })
        self.assertEqual(QA_SEED_VERSION, EXPECTED_RESULT['seed_version'])
        self.assertEqual(
            dict(
                InstructorAccount.objects.filter(email__in=QA_INSTRUCTOR_EMAILS)
                .values_list('email', 'user__password')
            ),
            password_hashes,
        )

    def test_seed_persists_browser_usable_published_and_response_graph(self):
        self.seed()

        self.assertEqual(
            set(Course.objects.filter(course_id__in=QA_COURSE_IDS).values_list(
                'course_id', flat=True,
            )),
            set(QA_COURSE_IDS),
        )
        self.assertEqual(
            CourseMembership.objects.filter(
                course__course_id__in=QA_COURSE_IDS,
                role=CourseMembership.ROLE_OWNER,
            ).count(),
            3,
        )
        reviewer = InstructorAccount.objects.get(email=QA_INSTRUCTOR_EMAILS[1])
        self.assertEqual(
            set(reviewer.institution_memberships.get().course_memberships.values_list(
                'course__course_id', flat=True,
            )),
            {QA_COURSE_IDS[1]},
        )

        draft = QuestionSetDraft.objects.get(public_id=QA_DRAFT_PUBLIC_ID)
        revision = QuestionSetRevision.objects.get(public_id=QA_REVISION_PUBLIC_ID)
        preview = PreviewSession.objects.get(public_id=QA_PREVIEW_PUBLIC_ID)
        link = QuestionSetSurvey.objects.select_related('survey').get(
            idempotency_key='qa-published-reflection-v1',
        )
        self.assertEqual(draft.question_set.public_id, QA_QUESTION_SET_PUBLIC_ID)
        self.assertEqual(draft.base_revision, revision)
        self.assertEqual(link.revision, revision)
        self.assertEqual(link.survey.public_id, QA_SURVEY_PUBLIC_ID)
        self.assertEqual(link.survey.mode, 'form')
        self.assertIsNone(link.survey.form_schema)
        self.assertIsNotNone(preview.completed_at)
        self.assertEqual(PreviewMessage.objects.filter(preview_session=preview).count(), 11)

        public = Client().get(
            '/datapipeline/api/get_feedback_gpt_by_public_id/',
            {'public_id': QA_SURVEY_PUBLIC_ID},
        )
        self.assertEqual(public.status_code, 200, public.content)
        self.assertEqual(
            public.json()['form_schema']['body'],
            revision.compiled_protocol,
        )

        response = ResponseSession.objects.get(public_id=QA_RESPONSE_PUBLIC_ID)
        messages = list(response.messages.order_by('sequence'))
        self.assertIsNotNone(response.completed_at)
        self.assertEqual([message.sequence for message in messages], list(range(1, 12)))
        self.assertTrue(all(not message.research_consent for message in messages))
        self.assertTrue(all(message.session_id.startswith('qa-') for message in messages))

        analysis = LEAIChatSession.objects.get(pk=QA_ANALYSIS_SESSION_ID)
        self.assertEqual(analysis.course.course_id, QA_COURSE_IDS[1])
        self.assertEqual(analysis.messages.count(), 2)
        self.assertEqual(
            list(analysis.messages.values_list('status', flat=True)),
            [LEAIChatMessage.STATUS_READY, LEAIChatMessage.STATUS_READY],
        )
        self.assertEqual(
            InstructorAuditEvent.objects.filter(
                event_id__in=QA_AUDIT_EVENT_IDS,
            ).count(),
            9,
        )

    def test_seeded_and_nonseed_revisions_remain_immutable_in_normal_paths(self):
        self.seed()
        revision = QuestionSetRevision.objects.get(public_id=QA_REVISION_PUBLIC_ID)

        revision.engine_version = 'changed'
        with self.assertRaises(ValidationError):
            revision.save()
        with self.assertRaises(ValidationError):
            QuestionSetRevision.objects.filter(pk=revision.pk).update(
                engine_version='changed',
            )
        with self.assertRaises(ValidationError):
            QuestionSetRevision.objects.filter(pk=revision.pk).delete()

    def test_confirmed_reset_preserves_similarly_named_nonowned_rows(self):
        self.seed()
        neighbor_institution = Institution.objects.create(
            slug='qa-ucsc-neighbor',
            name='QA Neighbor Institution',
        )
        neighbor_email = 'nearby@qa.invalid'
        neighbor_user = get_user_model().objects.create_user(
            username='qa-neighbor-user',
            email=neighbor_email,
            password=_runtime_password(),
        )
        neighbor_account = InstructorAccount.objects.create(
            user=neighbor_user,
            email=neighbor_email,
            display_name='QA Neighbor',
            must_change_password=False,
        )
        InstitutionMembership.objects.create(
            institution=neighbor_institution,
            instructor=neighbor_account,
        )
        neighbor_course = Course.objects.create(
            course_id='qa-active-course-copy',
            course_name='QA Active Course Copy',
            instructor_name='QA Neighbor',
            password=make_password(None),
            institution=neighbor_institution,
        )
        neighbor_survey = FeedbackGPT.objects.create(
            public_id='qa-active-w4-x',
            name='QA Neighbor Survey',
            instructions='Synthetic neighbor fixture.',
            course=neighbor_course,
        )
        seed_owner = InstructorAccount.objects.get(email=QA_INSTRUCTOR_EMAILS[0])
        neighbor_draft = create_draft(
            course=neighbor_course,
            actor=seed_owner,
            instructor_session=None,
            template_id='weekly-reflection',
        )
        neighbor_revision, _created = freeze_draft(
            draft_id=neighbor_draft.public_id,
            actor=seed_owner,
            instructor_session=None,
            expected_version=neighbor_draft.version,
        )

        result = self.seed(reset=True, confirm='qa')

        self.assertEqual(result, EXPECTED_RESULT)
        self.assertTrue(Institution.objects.filter(pk=neighbor_institution.pk).exists())
        self.assertTrue(InstructorAccount.objects.filter(pk=neighbor_account.pk).exists())
        self.assertTrue(Course.objects.filter(pk=neighbor_course.pk).exists())
        self.assertTrue(FeedbackGPT.objects.filter(pk=neighbor_survey.pk).exists())
        self.assertTrue(QuestionSetDraft.objects.filter(pk=neighbor_draft.pk).exists())
        self.assertTrue(QuestionSetRevision.objects.filter(pk=neighbor_revision.pk).exists())
        self.assertTrue(
            QuestionSetRevision.objects.filter(public_id=QA_REVISION_PUBLIC_ID).exists()
        )

    def test_confirmed_reset_preserves_nonseed_response_on_seed_survey(self):
        self.seed()
        survey = FeedbackGPT.objects.get(public_id=QA_SURVEY_PUBLIC_ID)
        survey_pk = survey.pk
        messages = persist_feedback_messages([{
            'session_id': 'browser-review-probe',
            'student_id': 'anonymous-review-probe',
            'sent_by': 'user-message',
            'content': 'Non-seed response that must survive a QA reset.',
            'gpt_used': survey.name,
            'gpt_id': survey.pk,
            'research_consent': False,
        }])
        response = messages[0].response_session
        response_pk = response.pk
        message_pk = messages[0].pk
        survey.name = 'Drifted QA survey name'
        survey.survey_label = 'Drifted QA survey label'
        survey.is_closed = True
        survey.save(update_fields=['name', 'survey_label', 'is_closed', 'updated_at'])

        result = self.seed(reset=True, confirm='qa')

        self.assertEqual(result, EXPECTED_RESULT)
        survey.refresh_from_db()
        self.assertEqual(survey.pk, survey_pk)
        self.assertEqual(survey.name, 'QA Active Course Week 4 Reflection')
        self.assertEqual(survey.survey_label, 'QA Active Course Week 4 Reflection')
        self.assertFalse(survey.is_closed)
        self.assertTrue(ResponseSession.objects.filter(pk=response_pk).exists())
        self.assertTrue(FeedbackMessage.objects.filter(pk=message_pk).exists())

    def test_confirmed_reset_preserves_later_revision_on_seed_question_set(self):
        self.seed()
        question_set = QuestionSet.objects.get(public_id=QA_QUESTION_SET_PUBLIC_ID)
        question_set_pk = question_set.pk
        seed_revision = QuestionSetRevision.objects.get(public_id=QA_REVISION_PUBLIC_ID)
        seed_revision_pk = seed_revision.pk
        draft = QuestionSetDraft.objects.get(public_id=QA_DRAFT_PUBLIC_ID)
        later_body = copy.deepcopy(draft.body)
        later_body['title'] = 'Later non-seed revision'
        draft = save_draft(
            draft_id=draft.public_id,
            actor=draft.updated_by,
            instructor_session=None,
            expected_version=draft.version,
            body=later_body,
        )
        later_revision, created = freeze_draft(
            draft_id=draft.public_id,
            actor=draft.updated_by,
            instructor_session=None,
            expected_version=draft.version,
        )
        self.assertTrue(created)
        later_revision_pk = later_revision.pk
        later_content_hash = later_revision.content_hash

        result = self.seed(reset=True, confirm='qa')

        self.assertEqual(result, EXPECTED_RESULT)
        self.assertTrue(QuestionSet.objects.filter(pk=question_set_pk).exists())
        self.assertTrue(QuestionSetRevision.objects.filter(pk=seed_revision_pk).exists())
        later_revision.refresh_from_db()
        self.assertEqual(later_revision.pk, later_revision_pk)
        self.assertEqual(later_revision.content_hash, later_content_hash)
        reset_draft = QuestionSetDraft.objects.get(public_id=QA_DRAFT_PUBLIC_ID)
        self.assertEqual(reset_draft.version, 1)
        self.assertEqual(reset_draft.base_revision_id, seed_revision_pk)
        self.assertEqual(reset_draft.body['title'], 'Weekly learning reflection')

    def test_ordinary_seed_does_not_privately_update_immutable_revision(self):
        with CaptureQueriesContext(connection) as queries:
            self.seed()

        revision_updates = [
            query['sql']
            for query in queries.captured_queries
            if query['sql'].lstrip().upper().startswith('UPDATE')
            and 'datapipeline_questionsetrevision' in query['sql'].lower()
        ]
        self.assertEqual(revision_updates, [])

    def test_same_action_audit_event_is_not_captured_or_mutated(self):
        racing = {}

        def create_with_same_action_event(**kwargs):
            target_id = uuid.uuid4()
            event = record_instructor_event(
                action=InstructorAuditEvent.ACTION_QUESTION_SET_DRAFT_CREATED,
                outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
                actor=kwargs['actor'],
                session=kwargs['instructor_session'],
                course=kwargs['course'],
                target_type='question_set',
                target_id=target_id,
                metadata={},
            )
            racing.update(event_id=event.event_id, target_id=str(target_id))
            return create_draft(**kwargs)

        with patch(
            'datapipeline.qa_seed.create_draft',
            side_effect=create_with_same_action_event,
        ):
            self.seed()

        event = InstructorAuditEvent.objects.get(event_id=racing['event_id'])
        self.assertEqual(event.target_id, racing['target_id'])
        self.assertNotIn(event.event_id, QA_AUDIT_EVENT_IDS)

    def test_seed_requires_runtime_credentials_for_missing_accounts(self):
        with self.assertRaises(QASeedError):
            self.seed(passwords=False)

        self.assertFalse(
            Institution.objects.filter(slug=QA_INSTITUTION_SLUG).exists()
        )

    def test_reset_requires_exact_confirmation(self):
        self.seed()

        with self.assertRaises(QASeedError):
            self.seed(reset=True, confirm='production')

        self.assertTrue(
            QuestionSetRevision.objects.filter(public_id=QA_REVISION_PUBLIC_ID).exists()
        )

    def test_count_mismatch_rolls_back_the_seed_transaction(self):
        mismatched = dict(EXPECTED_COUNTS)
        mismatched['courses'] -= 1

        with patch('datapipeline.qa_seed._persisted_counts', return_value=mismatched):
            with self.assertRaises(QASeedError):
                self.seed()

        self.assertFalse(
            Institution.objects.filter(slug=QA_INSTITUTION_SLUG).exists()
        )


class QASeedCommandTests(TestCase):
    def test_seed_refuses_non_qa_environment_before_requesting_credentials(self):
        with override_settings(LEAI_ENV='production'):
            with patch('getpass.getpass') as prompt:
                with self.assertRaises(CommandError):
                    call_command('seed_leai_qa')
        prompt.assert_not_called()

    @override_settings(LEAI_ENV='qa')
    def test_reset_requires_exact_command_confirmation(self):
        with patch('getpass.getpass') as prompt:
            with self.assertRaises(CommandError):
                call_command(
                    'seed_leai_qa',
                    reset=True,
                    confirm='production',
                )
        prompt.assert_not_called()

    @override_settings(LEAI_ENV='qa')
    def test_json_output_is_machine_readable_counts_without_credentials_or_text(self):
        primary_password = _runtime_password()
        reviewer_password = _runtime_password()
        stdout = io.StringIO()
        with patch.dict(os.environ, {
            'QA_PRIMARY_TEST_PASSWORD': primary_password,
            'QA_REVIEWER_TEST_PASSWORD': reviewer_password,
        }):
            call_command(
                'seed_leai_qa',
                primary_password_env='QA_PRIMARY_TEST_PASSWORD',
                reviewer_password_env='QA_REVIEWER_TEST_PASSWORD',
                as_json=True,
                stdout=stdout,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload, EXPECTED_RESULT)
        rendered = stdout.getvalue()
        self.assertNotIn(primary_password, rendered)
        self.assertNotIn(reviewer_password, rendered)
        self.assertNotIn('qa-anonymous', rendered)
        self.assertEqual(set(payload), set(EXPECTED_RESULT))
        self.assertTrue(all(
            isinstance(value, int)
            for key, value in payload.items()
            if key != 'seed_version'
        ))
