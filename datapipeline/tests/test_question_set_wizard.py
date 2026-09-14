import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

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
    ImmutableQuestionSetSurveyQuerySet,
    PreviewMessage,
    PreviewSession,
    QuestionSetDraft,
    QuestionSetRevision,
    QuestionSetSurvey,
    QuestionSet,
)
from datapipeline.instructor_audit import record_instructor_event
from datapipeline.question_sets import (
    QuestionSetError,
    create_draft as create_draft_service,
    create_survey_from_revision,
    freeze_draft as freeze_draft_service,
    issue_preview_capability,
    save_draft as save_draft_service,
)


def _survey_persistence_state():
    return {
        'surveys': list(
            FeedbackGPT.objects.order_by('pk').values(
                'pk',
                'public_id',
                'name',
                'survey_label',
                'instructions',
                'created_by',
                'course_id',
                'week_number',
                'opens_at',
                'expires_at',
                'is_closed',
                'anonymity_mode',
                'reporting_structure',
                'mode',
                'form_schema_id',
            )
        ),
        'links': list(
            QuestionSetSurvey.objects.order_by('pk').values(
                'pk',
                'survey_id',
                'revision_id',
                'preview_session_id',
                'completion_certificate_enabled',
                'parsed_document_download_enabled',
                'idempotency_key',
                'created_by_id',
            )
        ),
        'events': list(
            InstructorAuditEvent.objects.order_by('pk').values(
                'pk',
                'event_id',
                'action',
                'outcome',
                'actor_id',
                'session_id',
                'course_id',
                'course_id_snapshot',
                'target_type',
                'target_id',
                'metadata',
            )
        ),
    }


class QuestionSetWizardApiTests(TestCase):
    password = 'TemporaryPass123!'

    def setUp(self):
        self.client = Client()
        self.institution = Institution.objects.create(
            slug='ucsc',
            name='University of California, Santa Cruz',
        )
        self.user = get_user_model().objects.create_user(
            username='teacher@ucsc.edu',
            email='teacher@ucsc.edu',
            password=self.password,
        )
        self.account = InstructorAccount.objects.create(
            user=self.user,
            email='teacher@ucsc.edu',
            display_name='Prof. Test',
            must_change_password=False,
        )
        self.institution_membership = InstitutionMembership.objects.create(
            institution=self.institution,
            instructor=self.account,
        )
        self.course = Course.objects.create(
            course_id='wizard-course',
            course_name='Wizard Course',
            instructor_name='Prof. Test',
            password=make_password(None),
            institution=self.institution,
        )
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=self.institution_membership,
            role=CourseMembership.ROLE_OWNER,
        )
        login = self.post_json('/datapipeline/api/instructor_sessions/', {
            'email': self.account.email,
            'password': self.password,
        })
        self.assertEqual(login.status_code, 201)
        self.token = login.json()['token']

    def post_json(self, path, payload, *, token=None):
        headers = {}
        if token:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type='application/json',
            **headers,
        )

    def patch_json(self, path, payload):
        return self.client.patch(
            path,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

    def patch_json_with_token(self, path, payload, token):
        return self.client.patch(
            path,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def get_auth(self, path):
        return self.client.get(
            path,
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

    def create_draft(self, template_id='weekly-reflection'):
        response = self.post_json(
            '/datapipeline/api/question_set_drafts/',
            {
                'course_id': self.course.course_id,
                'template_id': template_id,
            },
            token=self.token,
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def freeze_draft(self, draft):
        response = self.post_json(
            f"/datapipeline/api/question_set_drafts/{draft['id']}/freeze/",
            {'expected_version': draft['version']},
            token=self.token,
        )
        self.assertEqual(response.status_code, 201)
        return response.json()['revision']

    def complete_preview(self, revision):
        capability = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/preview_capability/",
            {},
            token=self.token,
        )
        self.assertEqual(capability.status_code, 201)
        token = capability.json()['token']
        PreviewSession.objects.filter(token_digest=hashlib.sha256(
            token.encode('utf-8'),
        ).hexdigest()).update(ready_at=timezone.now())
        stored_revision = QuestionSetRevision.objects.get(public_id=revision['id'])
        for section in stored_revision.compiled_protocol['sections']:
            for field in section['fields']:
                asked = self.post_json(
                    f'/datapipeline/api/question_set_preview/{token}/messages/',
                    {
                        'role': 'assistant',
                        'content': field['label'],
                        'attribution': {
                            'form_schema_id': stored_revision.compiled_protocol['schema_id'],
                            'form_schema_version': stored_revision.compiled_protocol['version'],
                            'form_section_id': section['id'],
                            'form_field_id': field['id'],
                            'form_field_label': field['label'],
                            'form_response_phase': 'primary',
                        },
                    },
                )
                self.assertEqual(asked.status_code, 201)
                saved = self.post_json(
                    f'/datapipeline/api/question_set_preview/{token}/messages/',
                    {
                        'role': 'user',
                        'content': f"Practice answer for {field['id']}.",
                        'attribution': {
                            'form_schema_id': stored_revision.compiled_protocol['schema_id'],
                            'form_schema_version': stored_revision.compiled_protocol['version'],
                            'form_section_id': section['id'],
                            'form_field_id': field['id'],
                            'form_field_label': field['label'],
                            'form_response_phase': 'primary',
                        },
                    },
                )
                self.assertEqual(saved.status_code, 201)
        closing_question = self.post_json(
            f'/datapipeline/api/question_set_preview/{token}/messages/',
            {
                'role': 'assistant',
                'content': stored_revision.compiled_protocol['closing']['feedback_prompt'],
                'attribution': {},
            },
        )
        self.assertEqual(closing_question.status_code, 201)
        closing_answer = self.post_json(
            f'/datapipeline/api/question_set_preview/{token}/messages/',
            {'role': 'user', 'content': 'Practice closing answer.', 'attribution': {}},
        )
        self.assertEqual(closing_answer.status_code, 201)
        final_ack = self.post_json(
            f'/datapipeline/api/question_set_preview/{token}/messages/',
            {'role': 'assistant', 'content': 'Thank you.', 'attribution': {}},
        )
        self.assertEqual(final_ack.status_code, 201)
        completed = self.post_json(
            f'/datapipeline/api/question_set_preview/{token}/complete/',
            {},
        )
        self.assertEqual(completed.status_code, 200)
        return token

    def issue_preview(self, revision):
        capability = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/preview_capability/",
            {},
            token=self.token,
        )
        self.assertEqual(capability.status_code, 201)
        token = capability.json()['token']
        PreviewSession.objects.filter(token_digest=hashlib.sha256(
            token.encode('utf-8'),
        ).hexdigest()).update(ready_at=timezone.now())
        return token

    def create_survey(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        preview_token = self.complete_preview(revision)
        response = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            {
                'course_id': self.course.course_id,
                'idempotency_key': 'wizard-managed-survey',
                'preview_token': preview_token,
                'survey_label': 'Managed reflection',
                'week_number': 4,
                'opens_at': None,
                'expires_at': None,
            },
            token=self.token,
        )
        self.assertEqual(response.status_code, 201)
        return FeedbackGPT.objects.get(public_id=response.json()['public_id'])

    def create_service_survey(self):
        draft = self.create_draft()
        revision_payload = self.freeze_draft(draft)
        self.service_preview_token = self.complete_preview(revision_payload)
        revision = QuestionSetRevision.objects.get(
            public_id=revision_payload['id'],
        )
        audit_event_id = uuid.UUID('dd7dbeda-9f52-4411-a3bc-ab9a410112a8')
        link, created = create_survey_from_revision(
            revision=revision,
            actor=self.account,
            instructor_session=None,
            idempotency_key='service-survey-idempotency',
            survey_label='Service survey',
            week_number=4,
            opens_at=None,
            expires_at=None,
            preview_token=self.service_preview_token,
            survey_public_id='service-a',
            audit_event_id=audit_event_id,
        )
        self.assertTrue(created)
        return revision, link, audit_event_id

    def retry_service_survey(
        self,
        *,
        revision,
        instructor_session=None,
        survey_public_id=None,
        audit_event_id=None,
        preview_token=None,
    ):
        return create_survey_from_revision(
            revision=revision,
            actor=self.account,
            instructor_session=instructor_session,
            idempotency_key='service-survey-idempotency',
            survey_label='Ignored on idempotent retry',
            week_number=9,
            opens_at=None,
            expires_at=None,
            preview_token=preview_token or self.service_preview_token,
            survey_public_id=survey_public_id,
            audit_event_id=audit_event_id,
        )

    def create_conflicting_survey_event(
        self,
        *,
        event_id,
        link,
        action=InstructorAuditEvent.ACTION_SURVEY_CREATED,
        outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
        actor=True,
        course=None,
        target_id=None,
        metadata=None,
    ):
        if course is None:
            course = self.course
        if target_id is None:
            target_id = link.survey.pk
        if metadata is None:
            metadata = {'mode': 'form'}
        return record_instructor_event(
            event_id=event_id,
            action=action,
            outcome=outcome,
            actor=self.account if actor else None,
            session=None,
            course=course,
            target_type='survey',
            target_id=target_id,
            metadata=metadata,
        )

    def service_survey_state(self):
        return _survey_persistence_state()

    def test_templates_require_auth_and_only_offer_individual_reflections(self):
        denied = self.client.get('/datapipeline/api/question_set_templates/')
        self.assertEqual(denied.status_code, 401)

        response = self.get_auth('/datapipeline/api/question_set_templates/')

        self.assertEqual(response.status_code, 200)
        templates = response.json()['templates']
        self.assertEqual(len(templates), 3)
        self.assertEqual(
            {template['audience'] for template in templates},
            {'individual'},
        )
        self.assertTrue(all(template['body']['sections'] for template in templates))

    def test_cross_course_wizard_denial_is_audited(self):
        other_course = Course.objects.create(
            course_id='other-course',
            course_name='Other Course',
            instructor_name='Someone Else',
            password=make_password(None),
            institution=self.institution,
        )

        response = self.get_auth(
            '/datapipeline/api/question_set_drafts/'
            f'?course_id={other_course.course_id}'
        )

        self.assertEqual(response.status_code, 403)
        event = InstructorAuditEvent.objects.get(
            action=InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED,
        )
        self.assertEqual(event.actor, self.account)
        self.assertEqual(event.course, other_course)
        self.assertEqual(event.metadata['reason_code'], 'course_access_denied')

    def test_create_and_list_course_owned_draft_from_system_template(self):
        draft = self.create_draft()

        self.assertEqual(draft['course_id'], self.course.course_id)
        self.assertEqual(draft['template_id'], 'weekly-reflection')
        self.assertEqual(draft['version'], 1)
        self.assertEqual(draft['audience'], 'individual')
        self.assertGreaterEqual(len(draft['body']['sections']), 2)

        listed = self.get_auth(
            '/datapipeline/api/question_set_drafts/'
            f'?course_id={self.course.course_id}'
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item['id'] for item in listed.json()['drafts']], [draft['id']])
        event = InstructorAuditEvent.objects.get(
            action=InstructorAuditEvent.ACTION_QUESTION_SET_DRAFT_CREATED,
        )
        self.assertEqual(event.actor, self.account)
        self.assertEqual(event.course, self.course)

    def test_replacing_an_active_draft_requires_confirmation_and_keeps_history(self):
        first = self.create_draft()

        unconfirmed = self.post_json(
            '/datapipeline/api/question_set_drafts/',
            {
                'course_id': self.course.course_id,
                'template_id': 'mid-course-check-in',
            },
            token=self.token,
        )

        self.assertEqual(unconfirmed.status_code, 409)
        self.assertEqual(unconfirmed.json()['error'], 'active_draft_exists')

        confirmed = self.post_json(
            '/datapipeline/api/question_set_drafts/',
            {
                'course_id': self.course.course_id,
                'template_id': 'mid-course-check-in',
                'confirm_abandon_active': True,
            },
            token=self.token,
        )

        self.assertEqual(confirmed.status_code, 201)
        first_row = QuestionSetDraft.objects.select_related('question_set').get(
            public_id=first['id'],
        )
        self.assertEqual(
            getattr(first_row.question_set, 'workflow_status', None),
            'abandoned',
        )
        self.assertTrue(QuestionSetDraft.objects.filter(pk=first_row.pk).exists())
        listed = self.get_auth(
            '/datapipeline/api/question_set_drafts/'
            f'?course_id={self.course.course_id}'
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [item['id'] for item in listed.json()['drafts']],
            [confirmed.json()['id']],
        )
        self.assertTrue(InstructorAuditEvent.objects.filter(
            action='question_set.workflow_abandoned',
            target_id=str(first_row.question_set.public_id),
        ).exists())

    def test_successful_publication_completes_its_workflow(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        preview_token = self.complete_preview(revision)

        published = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            {
                'course_id': self.course.course_id,
                'idempotency_key': 'lifecycle-publication-survey',
                'preview_token': preview_token,
                'survey_label': 'Lifecycle publication',
                'week_number': 4,
                'opens_at': None,
                'expires_at': None,
            },
            token=self.token,
        )

        self.assertEqual(published.status_code, 201)
        question_set = QuestionSetRevision.objects.get(
            public_id=revision['id'],
        ).question_set
        self.assertEqual(
            getattr(question_set, 'workflow_status', None),
            'completed',
        )
        self.assertTrue(InstructorAuditEvent.objects.filter(
            action='question_set.workflow_completed',
            target_id=str(question_set.public_id),
        ).exists())

    def test_inactive_workflow_cannot_start_another_preview(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        for workflow_status in (
            QuestionSet.WORKFLOW_COMPLETED,
            QuestionSet.WORKFLOW_ABANDONED,
        ):
            with self.subTest(workflow_status=workflow_status):
                QuestionSet.objects.filter(
                    public_id=draft['question_set_id'],
                ).update(workflow_status=workflow_status)
                response = self.post_json(
                    f"/datapipeline/api/question_set_revisions/{revision['id']}/preview_capability/",
                    {},
                    token=self.token,
                )

                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['error'], 'workflow_not_active')

    def test_preview_capability_rechecks_after_concurrent_publication(self):
        draft = self.create_draft()
        revision_payload = self.freeze_draft(draft)
        preview_token = self.complete_preview(revision_payload)
        revision = QuestionSetRevision.objects.get(
            public_id=revision_payload['id'],
        )

        def publish_before_preview_service(*args, **kwargs):
            _link, created = create_survey_from_revision(
                revision=revision,
                actor=self.account,
                instructor_session=None,
                idempotency_key='preview-race-publication',
                survey_label='Publication that wins the race',
                week_number=4,
                opens_at=None,
                expires_at=None,
                preview_token=preview_token,
            )
            self.assertTrue(created)
            return issue_preview_capability(*args, **kwargs)

        with patch(
            'datapipeline.question_set_views.issue_preview_capability',
            side_effect=publish_before_preview_service,
        ):
            response = self.post_json(
                f"/datapipeline/api/question_set_revisions/{revision.public_id}/preview_capability/",
                {},
                token=self.token,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'workflow_not_active')
        self.assertEqual(QuestionSetSurvey.objects.count(), 1)
        self.assertEqual(PreviewSession.objects.count(), 1)

    def test_inactive_workflow_service_mutations_require_explicit_history_bypass(self):
        draft = create_draft_service(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            template_id='weekly-reflection',
        )
        QuestionSet.objects.filter(pk=draft.question_set_id).update(
            workflow_status=QuestionSet.WORKFLOW_ABANDONED,
        )

        with self.assertRaises(QuestionSetError) as saved:
            save_draft_service(
                draft_id=draft.public_id,
                actor=self.account,
                instructor_session=None,
                expected_version=draft.version,
                body=draft.body,
            )
        with self.assertRaises(QuestionSetError) as frozen:
            freeze_draft_service(
                draft_id=draft.public_id,
                actor=self.account,
                instructor_session=None,
                expected_version=draft.version,
            )

        self.assertEqual(saved.exception.code, 'workflow_not_active')
        self.assertEqual(frozen.exception.code, 'workflow_not_active')
        revision, created = freeze_draft_service(
            draft_id=draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=draft.version,
            allow_inactive_history=True,
        )
        self.assertTrue(created)
        self.assertEqual(revision.question_set_id, draft.question_set_id)

    def test_idempotent_publish_retry_rechecks_after_workflow_lock(self):
        draft = self.create_draft()
        revision_payload = self.freeze_draft(draft)
        preview_token = self.complete_preview(revision_payload)
        revision = QuestionSetRevision.objects.get(
            public_id=revision_payload['id'],
        )
        key = 'publish-recheck-after-lock'
        first, created = create_survey_from_revision(
            revision=revision,
            actor=self.account,
            instructor_session=None,
            idempotency_key=key,
            survey_label='First publication',
            week_number=4,
            opens_at=None,
            expires_at=None,
            preview_token=preview_token,
        )
        self.assertTrue(created)

        original_filter = type(QuestionSetSurvey.objects.all()).filter
        calls = 0

        def hide_only_the_prelock_lookup(queryset, *args, **kwargs):
            nonlocal calls
            if (
                queryset.model is QuestionSetSurvey
                and kwargs.get('idempotency_key') == key
            ):
                calls += 1
                if calls == 1:
                    return QuestionSetSurvey.objects.none()
            return original_filter(queryset, *args, **kwargs)

        with patch.object(
            type(QuestionSetSurvey.objects.all()),
            'filter',
            new=hide_only_the_prelock_lookup,
        ):
            retry, created = create_survey_from_revision(
                revision=revision,
                actor=self.account,
                instructor_session=None,
                idempotency_key=key,
                survey_label='Ignored retry values',
                week_number=9,
                opens_at=None,
                expires_at=None,
                preview_token=preview_token,
            )

        self.assertFalse(created)
        self.assertEqual(retry.pk, first.pk)
        self.assertEqual(calls, 2)

    def test_save_uses_optimistic_concurrency_and_preserves_protocol_structure(self):
        draft = self.create_draft()
        body = draft['body']
        body['title'] = 'My weekly learning check-in'
        body['sections'][0]['opening_prompt'] = 'What felt most useful this week?'

        saved = self.patch_json(
            f"/datapipeline/api/question_set_drafts/{draft['id']}/",
            {'expected_version': 1, 'body': body},
        )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()['version'], 2)
        self.assertEqual(saved.json()['body']['title'], 'My weekly learning check-in')

        stale = self.patch_json(
            f"/datapipeline/api/question_set_drafts/{draft['id']}/",
            {'expected_version': 1, 'body': body},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['error'], 'stale_draft')

        malformed = json.loads(json.dumps(saved.json()['body']))
        malformed['sections'][0]['id'] = 'changed-identity'
        rejected = self.patch_json(
            f"/datapipeline/api/question_set_drafts/{draft['id']}/",
            {'expected_version': 2, 'body': malformed},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()['error'], 'invalid_question_set')

    def test_freeze_is_immutable_and_content_idempotent(self):
        draft = self.create_draft()
        first = self.freeze_draft(draft)
        same = self.post_json(
            f"/datapipeline/api/question_set_drafts/{draft['id']}/freeze/",
            {'expected_version': draft['version']},
            token=self.token,
        )

        self.assertEqual(same.status_code, 200)
        self.assertEqual(same.json()['revision']['id'], first['id'])
        revision = QuestionSetRevision.objects.get(public_id=first['id'])
        revision.compiled_protocol = {'title': 'Mutated'}
        with self.assertRaises(ValidationError):
            revision.save()
        with self.assertRaises(ValidationError):
            QuestionSetRevision.objects.filter(pk=revision.pk).update(
                engine_version='mutated',
            )
        with self.assertRaises(ValidationError):
            QuestionSetRevision.objects.filter(pk=revision.pk).delete()

        revision.public_id = uuid.uuid4()
        with self.assertRaises(ValidationError):
            revision.save()
        revision.refresh_from_db()
        revision.created_at = revision.created_at - timedelta(seconds=1)
        with self.assertRaises(ValidationError):
            revision.save()

        revision.refresh_from_db()
        self.assertEqual(
            revision.compiled_protocol['effective_settings'],
            {
                'course_banner': None,
                'bot_display_name': 'LEAI',
                'referral_enabled': False,
                'referral_text': '',
                'identity_tracking_enabled': False,
                'completion_certificate_enabled': False,
                'parsed_document_download_enabled': False,
            },
        )

    def test_freeze_creates_requested_revision_and_audit_ids_atomically(self):
        draft = create_draft_service(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            template_id='weekly-reflection',
        )
        revision_id = uuid.UUID('81a151d4-5a71-4937-9a01-51d98456e08a')
        audit_id = uuid.UUID('05714ed9-cbaa-463c-946b-28d37ff6937f')

        revision, created = freeze_draft_service(
            draft_id=draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=draft.version,
            revision_public_id=revision_id,
            audit_event_id=audit_id,
        )

        self.assertTrue(created)
        self.assertEqual(revision.public_id, revision_id)
        event = InstructorAuditEvent.objects.get(event_id=audit_id)
        self.assertEqual(event.target_id, str(draft.question_set.public_id))

    def test_freeze_rejects_requested_revision_id_collision_without_side_effects(self):
        first_draft = create_draft_service(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            template_id='weekly-reflection',
        )
        collision_id = uuid.UUID('e4507ace-ae34-42d8-9037-0521cb8ff01a')
        freeze_draft_service(
            draft_id=first_draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=first_draft.version,
            revision_public_id=collision_id,
        )
        second_draft = create_draft_service(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            template_id='weekly-reflection',
            confirm_abandon_active=True,
        )

        with self.assertRaises(QuestionSetError) as raised:
            freeze_draft_service(
                draft_id=second_draft.public_id,
                actor=self.account,
                instructor_session=None,
                expected_version=second_draft.version,
                revision_public_id=collision_id,
            )

        self.assertEqual(raised.exception.code, 'revision_public_id_conflict')
        self.assertFalse(
            QuestionSetRevision.objects.filter(question_set=second_draft.question_set).exists()
        )
        second_draft.refresh_from_db()
        self.assertIsNone(second_draft.base_revision_id)

    def test_engine_upgrade_creates_a_new_revision_for_the_same_body(self):
        draft = self.create_draft()
        first = self.freeze_draft(draft)

        with patch('datapipeline.question_sets.ENGINE_VERSION', 'formmode-v2'):
            second_response = self.post_json(
                f"/datapipeline/api/question_set_drafts/{draft['id']}/freeze/",
                {'expected_version': draft['version']},
                token=self.token,
            )

        self.assertEqual(second_response.status_code, 201)
        second = second_response.json()['revision']
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual(second['revision_number'], 2)

    def test_preview_capability_is_short_lived_and_messages_are_isolated(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        capability = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/preview_capability/",
            {},
            token=self.token,
        )

        self.assertEqual(capability.status_code, 201)
        raw_token = capability.json()['token']
        session = PreviewSession.objects.get()
        self.assertNotEqual(raw_token, session.token_digest)
        self.assertEqual(
            hashlib.sha256(raw_token.encode('utf-8')).hexdigest(),
            session.token_digest,
        )
        self.assertLessEqual(session.expires_at, timezone.now() + timedelta(hours=24))
        self.assertIn('ready_at', capability.json())
        PreviewSession.objects.filter(pk=session.pk).update(ready_at=timezone.now())

        loaded = self.client.get(
            f'/datapipeline/api/question_set_preview/{raw_token}/'
        )
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.json()['mode'], 'form')
        self.assertEqual(
            loaded.json()['form_schema']['body']['title'],
            draft['body']['title'],
        )
        saved = self.post_json(
            f'/datapipeline/api/question_set_preview/{raw_token}/messages/',
            {'role': 'user', 'content': 'A practice answer.'},
        )
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(PreviewMessage.objects.count(), 1)
        self.assertEqual(FeedbackMessage.objects.count(), 0)

    def test_preview_capability_assigns_random_ready_at_at_both_boundaries(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        created_at = datetime(2026, 9, 13, 12, 0, tzinfo=datetime_timezone.utc)

        with patch(
            'datapipeline.question_sets.timezone.now',
            return_value=created_at,
        ), patch(
            'datapipeline.question_sets.secrets.randbelow',
            side_effect=[0, 1200],
        ):
            first = self.post_json(
                f"/datapipeline/api/question_set_revisions/{revision['id']}/preview_capability/",
                {},
                token=self.token,
            )
            second = self.post_json(
                f"/datapipeline/api/question_set_revisions/{revision['id']}/preview_capability/",
                {},
                token=self.token,
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        ready_times = list(
            PreviewSession.objects.order_by('id').values_list('ready_at', flat=True),
        )
        self.assertEqual(ready_times, [
            created_at + timedelta(seconds=2.6),
            created_at + timedelta(seconds=3.8),
        ])

    def test_public_preview_access_is_preparing_until_ready_at(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        created_at = datetime(2026, 9, 13, 12, 0, tzinfo=datetime_timezone.utc)

        with patch(
            'datapipeline.question_sets.timezone.now',
            return_value=created_at,
        ), patch('datapipeline.question_sets.secrets.randbelow', return_value=0):
            capability = self.post_json(
                f"/datapipeline/api/question_set_revisions/{revision['id']}/preview_capability/",
                {},
                token=self.token,
            )

        self.assertEqual(capability.status_code, 201)
        raw_token = capability.json()['token']
        with patch(
            'datapipeline.question_sets.timezone.now',
            return_value=created_at + timedelta(seconds=2.599),
        ):
            loaded = self.client.get(
                f'/datapipeline/api/question_set_preview/{raw_token}/',
            )
            saved = self.post_json(
                f'/datapipeline/api/question_set_preview/{raw_token}/messages/',
                {'role': 'user', 'content': 'Cannot be saved yet.'},
            )
            completed = self.post_json(
                f'/datapipeline/api/question_set_preview/{raw_token}/complete/',
                {},
            )

        for response in (loaded, saved, completed):
            self.assertEqual(response.status_code, 425)
            self.assertEqual(response.json()['error'], 'preview_preparing')
            self.assertEqual(
                response.json()['ready_at'],
                (created_at + timedelta(seconds=2.6)).isoformat(),
            )
            self.assertEqual(response.json()['retry_after_ms'], 1)
        self.assertEqual(PreviewMessage.objects.count(), 0)
        self.assertIsNone(PreviewSession.objects.get().completed_at)

    def test_public_preview_access_succeeds_at_ready_at(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        created_at = datetime(2026, 9, 13, 12, 0, tzinfo=datetime_timezone.utc)

        with patch(
            'datapipeline.question_sets.timezone.now',
            return_value=created_at,
        ), patch('datapipeline.question_sets.secrets.randbelow', return_value=0):
            capability = self.post_json(
                f"/datapipeline/api/question_set_revisions/{revision['id']}/preview_capability/",
                {},
                token=self.token,
            )

        raw_token = capability.json()['token']
        with patch(
            'datapipeline.question_sets.timezone.now',
            return_value=created_at + timedelta(seconds=2.6),
        ):
            loaded = self.client.get(
                f'/datapipeline/api/question_set_preview/{raw_token}/',
            )

        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.json()['mode'], 'form')

    def test_preview_cannot_complete_without_full_stored_conversation_evidence(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        raw_token = self.issue_preview(revision)

        completed = self.post_json(
            f'/datapipeline/api/question_set_preview/{raw_token}/complete/',
            {},
        )

        self.assertEqual(completed.status_code, 409)
        self.assertEqual(completed.json()['error'], 'preview_incomplete')
        self.assertIsNone(PreviewSession.objects.get().completed_at)

    def test_preview_rejects_attributed_answers_without_authored_questions(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        token = self.issue_preview(revision)
        stored = QuestionSetRevision.objects.get(public_id=revision['id'])
        protocol = stored.compiled_protocol
        for section in protocol['sections']:
            field = section['fields'][0]
            saved = self.post_json(
                f'/datapipeline/api/question_set_preview/{token}/messages/',
                {
                    'role': 'user',
                    'content': 'A caller-supplied attributed answer.',
                    'attribution': {
                        'form_schema_id': protocol['schema_id'],
                        'form_schema_version': protocol['version'],
                        'form_section_id': section['id'],
                        'form_field_id': field['id'],
                    },
                },
            )
            self.assertEqual(saved.status_code, 201)
        self.post_json(
            f'/datapipeline/api/question_set_preview/{token}/messages/',
            {'role': 'user', 'content': 'A closing answer.', 'attribution': {}},
        )
        self.post_json(
            f'/datapipeline/api/question_set_preview/{token}/messages/',
            {'role': 'assistant', 'content': 'A generic acknowledgement.', 'attribution': {}},
        )

        completed = self.post_json(
            f'/datapipeline/api/question_set_preview/{token}/complete/',
            {},
        )

        self.assertEqual(completed.status_code, 409)
        self.assertEqual(completed.json()['error'], 'preview_incomplete')

    def test_preview_completion_settings_default_to_certificate_only(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        self.issue_preview(revision)

        preview = PreviewSession.objects.get()

        self.assertTrue(preview.completion_certificate_enabled)
        self.assertFalse(preview.parsed_document_download_enabled)

    def test_preview_settings_patch_rejects_non_boolean_values(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        raw_token = self.issue_preview(revision)

        response = self.patch_json(
            f'/datapipeline/api/question_set_preview/{raw_token}/settings/',
            {'completion_certificate_enabled': 'false'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'invalid_preview_settings')
        preview = PreviewSession.objects.get()
        self.assertTrue(preview.completion_certificate_enabled)
        self.assertFalse(preview.parsed_document_download_enabled)

    def test_preview_settings_rechecks_expiry_after_lock(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        raw_token = self.issue_preview(revision)

        from datapipeline.question_sets import _preview_for_settings

        def expire_after_initial_lookup(token):
            preview = _preview_for_settings(token)
            PreviewSession.objects.filter(pk=preview.pk).update(
                expires_at=timezone.now() - timedelta(seconds=1),
            )
            return preview

        with patch(
            'datapipeline.question_sets._preview_for_settings',
            side_effect=expire_after_initial_lookup,
        ):
            response = self.patch_json(
                f'/datapipeline/api/question_set_preview/{raw_token}/settings/',
                {'completion_certificate_enabled': False},
            )

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()['error'], 'preview_expired')
        self.assertTrue(PreviewSession.objects.get().completion_certificate_enabled)

    def test_publication_rechecks_readiness_after_lock(self):
        draft = self.create_draft()
        revision_payload = self.freeze_draft(draft)
        raw_token = self.complete_preview(revision_payload)
        revision = QuestionSetRevision.objects.get(public_id=revision_payload['id'])

        from datapipeline.question_sets import get_preview

        def defer_after_initial_lookup(token):
            preview = get_preview(token)
            PreviewSession.objects.filter(pk=preview.pk).update(
                ready_at=timezone.now() + timedelta(seconds=30),
            )
            return preview

        with patch(
            'datapipeline.question_sets.get_preview',
            side_effect=defer_after_initial_lookup,
        ):
            response = self.post_json(
                f'/datapipeline/api/question_set_revisions/{revision.public_id}/surveys/',
                {
                    'course_id': self.course.course_id,
                    'idempotency_key': 'lock-readiness-race',
                    'preview_token': raw_token,
                    'survey_label': 'Readiness race',
                    'week_number': 4,
                    'opens_at': None,
                    'expires_at': None,
                },
                token=self.token,
            )

        self.assertEqual(response.status_code, 425)
        self.assertEqual(response.json()['error'], 'preview_preparing')
        self.assertEqual(FeedbackGPT.objects.count(), 0)

    def test_preview_settings_patch_requires_the_preview_owner(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        raw_token = self.issue_preview(revision)
        other_user = get_user_model().objects.create_user(
            username='other@ucsc.edu',
            email='other@ucsc.edu',
            password=self.password,
        )
        other_account = InstructorAccount.objects.create(
            user=other_user,
            email='other@ucsc.edu',
            display_name='Other Instructor',
            must_change_password=False,
        )
        other_membership = InstitutionMembership.objects.create(
            institution=self.institution,
            instructor=other_account,
        )
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=other_membership,
            role=CourseMembership.ROLE_INSTRUCTOR,
            can_publish=True,
        )
        other_client = Client()
        login = other_client.post(
            '/datapipeline/api/instructor_sessions/',
            data=json.dumps({
                'email': other_account.email,
                'password': self.password,
            }),
            content_type='application/json',
        )
        self.assertEqual(login.status_code, 201)

        response = other_client.patch(
            f'/datapipeline/api/question_set_preview/{raw_token}/settings/',
            data=json.dumps({'completion_certificate_enabled': False}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f"Bearer {login.json()['token']}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'preview_owner_mismatch')
        self.assertTrue(PreviewSession.objects.get().completion_certificate_enabled)

    def test_public_preview_uses_saved_completion_settings_after_ready(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        raw_token = self.issue_preview(revision)

        saved = self.patch_json(
            f'/datapipeline/api/question_set_preview/{raw_token}/settings/',
            {
                'completion_certificate_enabled': False,
                'parsed_document_download_enabled': True,
            },
        )
        self.assertEqual(saved.status_code, 200)

        public = self.client.get(
            f'/datapipeline/api/question_set_preview/{raw_token}/',
        )

        self.assertEqual(public.status_code, 200)
        self.assertFalse(public.json()['completion_certificate_enabled'])
        self.assertTrue(public.json()['parsed_document_download_enabled'])
        event = InstructorAuditEvent.objects.get(
            action=InstructorAuditEvent.ACTION_QUESTION_SET_PREVIEW_SETTINGS_UPDATED,
        )
        self.assertEqual(event.metadata, {
            'completion_certificate_enabled': False,
            'parsed_document_download_enabled': True,
        })
        self.assertNotIn(raw_token, json.dumps(event.metadata))

    def test_publication_requires_the_exact_completed_preview_token(self):
        draft = self.create_draft()
        first_revision = self.freeze_draft(draft)
        first_preview_token = self.complete_preview(first_revision)
        changed_body = json.loads(json.dumps(draft['body']))
        changed_body['title'] = 'A revised weekly reflection'
        saved = self.patch_json(
            f"/datapipeline/api/question_set_drafts/{draft['id']}/",
            {'expected_version': draft['version'], 'body': changed_body},
        )
        self.assertEqual(saved.status_code, 200)
        second_revision = self.freeze_draft(saved.json())

        response = self.post_json(
            f"/datapipeline/api/question_set_revisions/{second_revision['id']}/surveys/",
            {
                'course_id': self.course.course_id,
                'idempotency_key': 'exact-preview-revision',
                'preview_token': first_preview_token,
                'survey_label': 'Second revision reflection',
                'week_number': 4,
                'opens_at': None,
                'expires_at': None,
            },
            token=self.token,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'preview_revision_mismatch')
        self.assertEqual(QuestionSetSurvey.objects.count(), 0)

    def test_idempotent_publication_rejects_a_different_preview_settings_snapshot(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        first_preview_token = self.complete_preview(revision)
        second_preview_token = self.complete_preview(revision)
        changed = self.patch_json(
            f'/datapipeline/api/question_set_preview/{second_preview_token}/settings/',
            {
                'completion_certificate_enabled': False,
                'parsed_document_download_enabled': True,
            },
        )
        self.assertEqual(changed.status_code, 200)
        payload = {
            'course_id': self.course.course_id,
            'idempotency_key': 'same-key-different-settings',
            'preview_token': first_preview_token,
            'survey_label': 'Published settings snapshot',
            'week_number': 4,
            'opens_at': None,
            'expires_at': None,
        }
        created = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            payload,
            token=self.token,
        )
        self.assertEqual(created.status_code, 201)
        payload['preview_token'] = second_preview_token

        repeated = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            payload,
            token=self.token,
        )

        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(repeated.json()['error'], 'idempotency_key_conflict')
        self.assertEqual(QuestionSetSurvey.objects.count(), 1)

    def test_published_survey_uses_its_preview_completion_settings(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        preview_token = self.complete_preview(revision)
        saved = self.patch_json(
            f'/datapipeline/api/question_set_preview/{preview_token}/settings/',
            {
                'completion_certificate_enabled': False,
                'parsed_document_download_enabled': True,
            },
        )
        self.assertEqual(saved.status_code, 200)
        published = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            {
                'course_id': self.course.course_id,
                'idempotency_key': 'published-preview-settings',
                'preview_token': preview_token,
                'survey_label': 'Published preview settings',
                'week_number': 4,
                'opens_at': None,
                'expires_at': None,
            },
            token=self.token,
        )
        self.assertEqual(published.status_code, 201)
        self.course.completion_certificate_enabled = True
        self.course.parsed_document_download_enabled = False
        self.course.save(update_fields=[
            'completion_certificate_enabled',
            'parsed_document_download_enabled',
        ])

        public = self.client.get(
            '/datapipeline/api/get_feedback_gpt_by_public_id/',
            {'public_id': published.json()['public_id']},
        )

        self.assertEqual(public.status_code, 200)
        self.assertFalse(public.json()['completion_certificate_enabled'])
        self.assertTrue(public.json()['parsed_document_download_enabled'])
        link = QuestionSetSurvey.objects.get(survey_id=published.json()['id'])
        link.completion_certificate_enabled = True
        with self.assertRaises(ValidationError):
            link.save()

    def test_survey_creation_requires_completed_exact_revision_and_is_idempotent(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        capability = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/preview_capability/",
            {},
            token=self.token,
        )
        self.assertEqual(capability.status_code, 201)
        preview_token = capability.json()['token']
        payload = {
            'course_id': self.course.course_id,
            'idempotency_key': 'wizard-create-one-survey',
            'preview_token': preview_token,
            'survey_label': 'Week 3 reflection',
            'week_number': 3,
            'opens_at': None,
            'expires_at': None,
        }

        blocked = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            payload,
            token=self.token,
        )
        self.assertEqual(blocked.status_code, 425)
        self.assertEqual(blocked.json()['error'], 'preview_preparing')
        PreviewSession.objects.filter(token_digest=hashlib.sha256(
            preview_token.encode('utf-8'),
        ).hexdigest()).update(ready_at=timezone.now())
        blocked = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            payload,
            token=self.token,
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()['error'], 'preview_incomplete')

        payload['preview_token'] = self.complete_preview(revision)
        created = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            payload,
            token=self.token,
        )
        repeated = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            payload,
            token=self.token,
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(created.json()['public_id'], repeated.json()['public_id'])
        self.assertEqual(FeedbackGPT.objects.count(), 1)
        survey = FeedbackGPT.objects.get()
        self.assertIsNone(survey.expires_at)
        self.assertIsNone(created.json()['expires_at'])
        link = QuestionSetSurvey.objects.select_related('survey', 'revision').get()
        self.assertEqual(link.revision.public_id.hex, revision['id'].replace('-', ''))
        self.assertIsNone(link.survey.form_schema)

        public = self.client.get(
            '/datapipeline/api/get_feedback_gpt_by_public_id/',
            {'public_id': created.json()['public_id']},
        )
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.json()['form_schema']['body'], link.revision.compiled_protocol)
        self.assertEqual(
            public.json()['form_schema_id'],
            f'question-set:{link.revision.question_set.public_id}:v1',
        )

    def test_service_survey_retry_accepts_matching_optional_ids(self):
        revision, link, audit_event_id = self.create_service_survey()
        counts_before = (
            FeedbackGPT.objects.count(),
            QuestionSetSurvey.objects.count(),
            InstructorAuditEvent.objects.count(),
        )

        repeated, created = self.retry_service_survey(
            revision=revision,
            survey_public_id=link.survey.public_id,
            audit_event_id=audit_event_id,
        )

        self.assertFalse(created)
        self.assertEqual(repeated.pk, link.pk)
        self.assertEqual(counts_before, (
            FeedbackGPT.objects.count(),
            QuestionSetSurvey.objects.count(),
            InstructorAuditEvent.objects.count(),
        ))

    def test_service_survey_retry_rejects_different_optional_ids(self):
        revision, link, _audit_event_id = self.create_service_survey()
        second_audit_id = uuid.UUID('eb5431c8-0299-4054-935f-8d8469002e4f')
        counts_before = (
            FeedbackGPT.objects.count(),
            QuestionSetSurvey.objects.count(),
            InstructorAuditEvent.objects.count(),
        )

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id='service-b',
                audit_event_id=second_audit_id,
            )

        self.assertEqual(raised.exception.code, 'survey_public_id_conflict')
        link.refresh_from_db()
        self.assertEqual(link.survey.public_id, 'service-a')
        self.assertFalse(
            InstructorAuditEvent.objects.filter(event_id=second_audit_id).exists()
        )
        self.assertEqual(counts_before, (
            FeedbackGPT.objects.count(),
            QuestionSetSurvey.objects.count(),
            InstructorAuditEvent.objects.count(),
        ))

    def test_service_survey_retry_rejects_missing_audit_event(self):
        revision, _link, _audit_event_id = self.create_service_survey()
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id='service-a',
                audit_event_id=uuid.UUID('066d68af-50fd-48d8-9434-050a39a33f5b'),
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_not_found')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_optional_id_types(self):
        revision, _link, audit_event_id = self.create_service_survey()
        state_before = self.service_survey_state()
        invalid_cases = (
            ({'survey_public_id': uuid.uuid4(), 'audit_event_id': audit_event_id},
             'invalid_survey_public_id'),
            ({'survey_public_id': 'service-a', 'audit_event_id': str(audit_event_id)},
             'invalid_audit_event_id'),
        )

        for optional_ids, error_code in invalid_cases:
            with self.subTest(error_code=error_code):
                with self.assertRaises(QuestionSetError) as raised:
                    self.retry_service_survey(
                        revision=revision,
                        **optional_ids,
                    )
                self.assertEqual(raised.exception.code, error_code)
                self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_action_event(self):
        revision, link, _audit_event_id = self.create_service_survey()
        conflicting_id = uuid.UUID('fa25967f-96cf-4ba8-8e3a-aea9d6da8d46')
        self.create_conflicting_survey_event(
            event_id=conflicting_id,
            link=link,
            action=InstructorAuditEvent.ACTION_SURVEY_UPDATED,
            metadata={'changed_fields': []},
        )
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id='service-a',
                audit_event_id=conflicting_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_outcome_event(self):
        revision, link, _audit_event_id = self.create_service_survey()
        conflicting_id = uuid.UUID('27021f45-04af-4498-bf00-5b634c370cee')
        self.create_conflicting_survey_event(
            event_id=conflicting_id,
            link=link,
            outcome=InstructorAuditEvent.OUTCOME_FAILED,
        )
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id='service-a',
                audit_event_id=conflicting_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_target_event(self):
        revision, link, _audit_event_id = self.create_service_survey()
        conflicting_id = uuid.UUID('3c44cb69-44d9-43d8-80d1-589a4143a776')
        self.create_conflicting_survey_event(
            event_id=conflicting_id,
            link=link,
            target_id=link.survey.pk + 1000,
        )
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id='service-a',
                audit_event_id=conflicting_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_actor_event(self):
        revision, link, _audit_event_id = self.create_service_survey()
        conflicting_id = uuid.UUID('e1aa68d2-c6b2-4fc6-a623-263745243a21')
        self.create_conflicting_survey_event(
            event_id=conflicting_id,
            link=link,
            actor=False,
        )
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id='service-a',
                audit_event_id=conflicting_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_course_event(self):
        other_course = Course.objects.create(
            course_id='other-wizard-course',
            course_name='Other Wizard Course',
            instructor_name='Prof. Test',
            password=make_password(None),
            institution=self.institution,
        )
        revision, link, _audit_event_id = self.create_service_survey()
        conflicting_id = uuid.UUID('78d10c72-9764-453b-b570-0d817bc0f9de')
        self.create_conflicting_survey_event(
            event_id=conflicting_id,
            link=link,
            course=other_course,
        )
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id='service-a',
                audit_event_id=conflicting_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_metadata_event(self):
        revision, link, _audit_event_id = self.create_service_survey()
        conflicting_id = uuid.UUID('c317ec32-852c-45e0-b6e9-0d36f907bce5')
        self.create_conflicting_survey_event(
            event_id=conflicting_id,
            link=link,
            metadata={'mode': 'general'},
        )
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id='service-a',
                audit_event_id=conflicting_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_instructor_session(self):
        revision, link, audit_event_id = self.create_service_survey()
        instructor_session = InstructorSession.objects.get(
            instructor=self.account,
            revoked_at__isnull=True,
        )
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                instructor_session=instructor_session,
                survey_public_id=link.survey.public_id,
                audit_event_id=audit_event_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_course_id_snapshot(self):
        revision, link, audit_event_id = self.create_service_survey()
        InstructorAuditEvent.objects.filter(event_id=audit_event_id).update(
            course_id_snapshot='wrong-course-snapshot',
        )
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id=link.survey.public_id,
                audit_event_id=audit_event_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_service_survey_retry_rejects_wrong_target_type(self):
        revision, link, audit_event_id = self.create_service_survey()
        InstructorAuditEvent.objects.filter(event_id=audit_event_id).update(
            target_type='course',
        )
        state_before = self.service_survey_state()

        with self.assertRaises(QuestionSetError) as raised:
            self.retry_service_survey(
                revision=revision,
                survey_public_id=link.survey.public_id,
                audit_event_id=audit_event_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertEqual(self.service_survey_state(), state_before)

    def test_expired_preview_capability_cannot_be_used_or_completed(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        token = self.complete_preview(revision)
        PreviewSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        loaded = self.client.get(
            f'/datapipeline/api/question_set_preview/{token}/'
        )
        completed = self.post_json(
            f'/datapipeline/api/question_set_preview/{token}/complete/',
            {},
        )

        self.assertEqual(loaded.status_code, 410)
        self.assertEqual(completed.status_code, 410)
        self.assertEqual(loaded.json()['error'], 'preview_expired')
        self.assertEqual(completed.json()['error'], 'preview_expired')

    def test_legacy_edit_endpoint_cannot_mutate_question_set_survey(self):
        survey = self.create_survey()

        response = self.post_json(
            '/datapipeline/api/update_survey/',
            {
                'survey_id': survey.id,
                'survey_label': 'Mutated outside the wizard',
            },
            token=self.token,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'question_set_managed')
        survey.refresh_from_db()
        self.assertEqual(survey.survey_label, 'Managed reflection')

    def test_legacy_clone_endpoint_cannot_copy_question_set_survey(self):
        survey = self.create_survey()

        response = self.post_json(
            '/datapipeline/api/clone_survey/',
            {'survey_id': survey.id},
            token=self.token,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'question_set_managed')
        self.assertEqual(FeedbackGPT.objects.count(), 1)

    def test_legacy_delete_endpoint_cannot_remove_question_set_survey(self):
        survey = self.create_survey()

        response = self.post_json(
            '/datapipeline/api/delete_survey/',
            {'survey_id': survey.id},
            token=self.token,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'question_set_managed')
        self.assertTrue(FeedbackGPT.objects.filter(pk=survey.pk).exists())
        self.assertTrue(QuestionSetSurvey.objects.filter(survey=survey).exists())

        with self.assertRaises(ProtectedError):
            survey.delete()

    def test_published_revision_keeps_frozen_student_settings(self):
        survey = self.create_survey()
        self.course.bot_display_name = 'Changed later'
        self.course.referral_enabled = True
        self.course.referral_text = 'a live course contact'
        self.course.identity_tracking_enabled = True
        self.course.completion_certificate_enabled = True
        self.course.parsed_document_download_enabled = True
        self.course.banner_enabled = True
        self.course.banner_text = 'A later course banner'
        self.course.save()

        public = self.client.get(
            '/datapipeline/api/get_feedback_gpt_by_public_id/',
            {'public_id': survey.public_id, 'session_id': 'student-session'},
        )

        self.assertEqual(public.status_code, 200)
        self.assertIsNone(public.json()['course_banner'])
        self.assertEqual(public.json()['bot_display_name'], 'LEAI')
        self.assertFalse(public.json()['referral_enabled'])
        self.assertEqual(public.json()['referral_text'], '')
        self.assertFalse(public.json()['identity_tracking_enabled'])
        self.assertTrue(public.json()['completion_certificate_enabled'])
        self.assertFalse(public.json()['parsed_document_download_enabled'])

    def test_preview_and_default_label_use_frozen_revision_title(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        raw_token = self.issue_preview(revision)
        mutable_question_set = QuestionSetRevision.objects.get(
            public_id=revision['id'],
        ).question_set
        mutable_question_set.title = 'Later draft title'
        mutable_question_set.save(update_fields=['title'])

        preview = self.client.get(
            f'/datapipeline/api/question_set_preview/{raw_token}/'
        )

        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()['name'], draft['body']['title'])


class QuestionSetWorkflowMigrationTests(TransactionTestCase):
    migrate_from = [('datapipeline', '0048_question_set_wizard')]
    migrate_to = [('datapipeline', '0049_question_set_workflow_lifecycle')]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model('auth', 'User')
        Institution = old_apps.get_model('datapipeline', 'Institution')
        InstructorAccount = old_apps.get_model('datapipeline', 'InstructorAccount')
        Course = old_apps.get_model('datapipeline', 'Course')
        QuestionSet = old_apps.get_model('datapipeline', 'QuestionSet')
        QuestionSetDraft = old_apps.get_model('datapipeline', 'QuestionSetDraft')
        QuestionSetRevision = old_apps.get_model(
            'datapipeline', 'QuestionSetRevision',
        )
        QuestionSetSurvey = old_apps.get_model('datapipeline', 'QuestionSetSurvey')
        FeedbackGPT = old_apps.get_model('datapipeline', 'FeedbackGPT')

        institution = Institution.objects.create(
            slug='lifecycle-migration',
            name='Lifecycle Migration University',
        )
        user = User.objects.create(username='lifecycle-migration-user')
        owner = InstructorAccount.objects.create(
            user=user,
            email='lifecycle-migration@example.invalid',
            display_name='Lifecycle Migration Owner',
            must_change_password=False,
        )
        course = Course.objects.create(
            course_id='lifecycle-migration-course',
            course_name='Lifecycle Migration Course',
            instructor_name='Lifecycle Migration Owner',
            password='!',
            institution=institution,
        )

        def old_workflow(title):
            question_set = QuestionSet.objects.create(
                course=course,
                owner=owner,
                template_id='weekly-reflection',
                title=title,
                audience='individual',
            )
            draft = QuestionSetDraft.objects.create(
                question_set=question_set,
                body={'title': title, 'sections': []},
                updated_by=owner,
            )
            return question_set, draft

        older, older_draft = old_workflow('Older unfinished')
        newest, newest_draft = old_workflow('Newest unfinished')
        published, _published_draft = old_workflow('Published workflow')
        QuestionSetDraft.objects.filter(pk=older_draft.pk).update(
            updated_at=timezone.now() - timedelta(days=2),
        )
        QuestionSetDraft.objects.filter(pk=newest_draft.pk).update(
            updated_at=timezone.now() - timedelta(days=1),
        )
        revision = QuestionSetRevision.objects.create(
            question_set=published,
            revision_number=1,
            source_draft_version=1,
            canonical_body={'title': 'Published workflow'},
            compiled_protocol={'title': 'Published workflow'},
            content_hash='a' * 64,
            created_by=owner,
        )
        survey = FeedbackGPT.objects.create(
            public_id='life-migrate-01',
            name='Lifecycle migration survey',
            survey_label='Lifecycle migration survey',
            instructions='Synthetic migration fixture.',
            created_by=owner.display_name,
            course=course,
            mode='form',
        )
        QuestionSetSurvey.objects.create(
            survey=survey,
            revision=revision,
            idempotency_key='lifecycle-migration-key',
            created_by=owner,
        )
        self.question_set_ids = {
            'older': older.pk,
            'newest': newest.pk,
            'published': published.pk,
        }

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def test_migration_completes_published_workflows_and_keeps_one_unpublished_active(self):
        QuestionSet = self.apps.get_model('datapipeline', 'QuestionSet')

        statuses = {
            name: QuestionSet.objects.get(pk=pk).workflow_status
            for name, pk in self.question_set_ids.items()
        }

        self.assertEqual(statuses, {
            'older': 'abandoned',
            'newest': 'active',
            'published': 'completed',
        })


class QuestionSetCompletionSettingsMigrationTests(TransactionTestCase):
    migrate_from = [('datapipeline', '0050_preview_session_ready_at')]
    migrate_to = [('datapipeline', '0051_question_set_completion_settings')]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model('auth', 'User')
        Institution = old_apps.get_model('datapipeline', 'Institution')
        InstructorAccount = old_apps.get_model('datapipeline', 'InstructorAccount')
        Course = old_apps.get_model('datapipeline', 'Course')
        QuestionSet = old_apps.get_model('datapipeline', 'QuestionSet')
        QuestionSetRevision = old_apps.get_model(
            'datapipeline', 'QuestionSetRevision',
        )
        QuestionSetSurvey = old_apps.get_model('datapipeline', 'QuestionSetSurvey')
        FeedbackGPT = old_apps.get_model('datapipeline', 'FeedbackGPT')

        institution = Institution.objects.create(
            slug='completion-settings-migration',
            name='Completion Settings Migration University',
        )
        user = User.objects.create(username='completion-settings-migration-user')
        owner = InstructorAccount.objects.create(
            user=user,
            email='completion-settings-migration@example.invalid',
            display_name='Completion Settings Migration Owner',
            must_change_password=False,
        )
        course = Course.objects.create(
            course_id='completion-settings-migration-course',
            course_name='Completion Settings Migration Course',
            instructor_name='Completion Settings Migration Owner',
            password='!',
            institution=institution,
        )
        question_set = QuestionSet.objects.create(
            course=course,
            owner=owner,
            template_id='weekly-reflection',
            title='Historical settings',
            audience='individual',
        )
        revision = QuestionSetRevision.objects.create(
            question_set=question_set,
            revision_number=1,
            source_draft_version=1,
            canonical_body={'title': 'Historical settings'},
            compiled_protocol={
                'title': 'Historical settings',
                'effective_settings': {
                    'completion_certificate_enabled': False,
                    'parsed_document_download_enabled': False,
                },
            },
            content_hash='b' * 64,
            created_by=owner,
        )
        survey = FeedbackGPT.objects.create(
            public_id='completion-mig01',
            name='Historical completion settings survey',
            survey_label='Historical completion settings survey',
            instructions='Synthetic migration fixture.',
            created_by=owner.display_name,
            course=course,
            mode='form',
        )
        link = QuestionSetSurvey.objects.create(
            survey=survey,
            revision=revision,
            idempotency_key='completion-settings-migration-key',
            created_by=owner,
        )
        self.link_id = link.pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def test_migration_backfills_historical_revision_settings(self):
        QuestionSetSurvey = self.apps.get_model('datapipeline', 'QuestionSetSurvey')

        link = QuestionSetSurvey.objects.get(pk=self.link_id)

        self.assertFalse(link.completion_certificate_enabled)
        self.assertFalse(link.parsed_document_download_enabled)


class QuestionSetSurveyRaceTests(TransactionTestCase):
    idempotency_key = 'concurrent-survey-idempotency'

    def setUp(self):
        executor = MigrationExecutor(connections['default'])
        executor.migrate(executor.loader.graph.leaf_nodes())
        self.institution = Institution.objects.create(
            slug='race-institution',
            name='Race Institution',
        )
        self.user = get_user_model().objects.create_user(
            username='race-instructor@qa.invalid',
            email='race-instructor@qa.invalid',
        )
        self.account = InstructorAccount.objects.create(
            user=self.user,
            email='race-instructor@qa.invalid',
            display_name='Race Instructor',
            must_change_password=False,
        )
        institution_membership = InstitutionMembership.objects.create(
            institution=self.institution,
            instructor=self.account,
        )
        self.course = Course.objects.create(
            course_id='race-course',
            course_name='Race Course',
            instructor_name='Race Instructor',
            password=make_password(None),
            institution=self.institution,
        )
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=institution_membership,
            role=CourseMembership.ROLE_OWNER,
        )
        draft = create_draft_service(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            template_id='weekly-reflection',
        )
        self.revision, created = freeze_draft_service(
            draft_id=draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=draft.version,
        )
        self.assertTrue(created)
        PreviewSession.objects.create(
            revision=self.revision,
            instructor=self.account,
            token_digest=hashlib.sha256(b'race-preview').hexdigest(),
            expires_at=timezone.now() + timedelta(hours=1),
            ready_at=timezone.now(),
            completed_at=timezone.now(),
        )
        self.preview_token = 'race-preview'
        self.winner_survey = FeedbackGPT.objects.create(
            public_id='race-winner',
            name='Concurrent winner',
            survey_label='Concurrent winner',
            instructions='Concurrent winner instructions.',
            created_by=self.account.display_name,
            course=self.course,
            week_number=4,
            is_closed=False,
            anonymity_mode='anonymous',
            reporting_structure='',
            mode='form',
            form_schema=None,
        )
        self.race_winner_state = None

    def call_with_concurrent_winner(
        self,
        *,
        supplied_audit_event_id,
        winner_audit_event_id,
    ):
        original_create = QuestionSetSurvey.objects.create
        preview = PreviewSession.objects.get(
            token_digest=hashlib.sha256(self.preview_token.encode('utf-8')).hexdigest(),
        )
        original_create(
            survey=self.winner_survey,
            revision=self.revision,
            preview_session=preview,
            completion_certificate_enabled=True,
            parsed_document_download_enabled=False,
            idempotency_key=self.idempotency_key,
            created_by=self.account,
        )
        record_instructor_event(
            event_id=winner_audit_event_id,
            action=InstructorAuditEvent.ACTION_SURVEY_CREATED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            actor=self.account,
            session=None,
            course=self.course,
            target_type='survey',
            target_id=self.winner_survey.pk,
            metadata={'mode': 'form'},
        )
        self.race_winner_state = _survey_persistence_state()
        original_filter = ImmutableQuestionSetSurveyQuerySet.filter
        hidden_winner_lookups = 0

        def hide_winner_until_insert_conflict(*args, **kwargs):
            nonlocal hidden_winner_lookups
            if kwargs.get('idempotency_key') == self.idempotency_key:
                hidden_winner_lookups += 1
                if hidden_winner_lookups <= 2:
                    return original_filter(args[0], pk__in=[])
            return original_filter(*args, **kwargs)

        def collide_at_link_create(**_kwargs):
            raise IntegrityError('forced concurrent link winner')

        with (
            patch.object(
                ImmutableQuestionSetSurveyQuerySet,
                'filter',
                autospec=True,
                side_effect=hide_winner_until_insert_conflict,
            ),
            patch.object(QuestionSetSurvey.objects, 'create', side_effect=collide_at_link_create),
        ):
            result = create_survey_from_revision(
                revision=self.revision,
                actor=self.account,
                instructor_session=None,
                idempotency_key=self.idempotency_key,
                survey_label='Losing survey',
                week_number=9,
                opens_at=None,
                expires_at=None,
                preview_token=self.preview_token,
                audit_event_id=supplied_audit_event_id,
            )
        self.assertEqual(hidden_winner_lookups, 3)
        return result

    def test_concurrent_winner_retry_rejects_mismatched_audit_id_without_mutation(self):
        mismatched_event_id = uuid.UUID('0e4069ef-7875-465d-a2b7-a3911ae422a1')
        winner_event_id = uuid.UUID('513e62df-5b64-4b75-b3e6-039387403f17')
        record_instructor_event(
            event_id=mismatched_event_id,
            action=InstructorAuditEvent.ACTION_SURVEY_UPDATED,
            outcome=InstructorAuditEvent.OUTCOME_SUCCESS,
            actor=self.account,
            session=None,
            course=self.course,
            target_type='survey',
            target_id=self.winner_survey.pk,
            metadata={'changed_fields': []},
        )

        with self.assertRaises(QuestionSetError) as raised:
            self.call_with_concurrent_winner(
                supplied_audit_event_id=mismatched_event_id,
                winner_audit_event_id=winner_event_id,
            )

        self.assertEqual(raised.exception.code, 'audit_event_id_conflict')
        self.assertIsNotNone(self.race_winner_state)
        self.assertEqual(_survey_persistence_state(), self.race_winner_state)
        self.assertEqual(FeedbackGPT.objects.count(), 1)
        self.assertEqual(QuestionSetSurvey.objects.count(), 1)
        self.assertEqual(InstructorAuditEvent.objects.count(), 4)

    def test_concurrent_winner_retry_accepts_matching_audit_id_without_mutation(self):
        winner_event_id = uuid.UUID('b709ce1f-f5cf-4a42-b7fa-f1460eab7250')

        link, created = self.call_with_concurrent_winner(
            supplied_audit_event_id=winner_event_id,
            winner_audit_event_id=winner_event_id,
        )

        self.assertFalse(created)
        self.assertEqual(link.survey_id, self.winner_survey.pk)
        self.assertIsNotNone(self.race_winner_state)
        self.assertEqual(_survey_persistence_state(), self.race_winner_state)
        self.assertEqual(FeedbackGPT.objects.count(), 1)
        self.assertEqual(QuestionSetSurvey.objects.count(), 1)
        self.assertEqual(InstructorAuditEvent.objects.count(), 3)
