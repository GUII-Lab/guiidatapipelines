import hashlib
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
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
    PreviewMessage,
    PreviewSession,
    QuestionSetDraft,
    QuestionSetRevision,
    QuestionSetSurvey,
)


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
        return capability.json()['token']

    def create_survey(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        self.complete_preview(revision)
        response = self.post_json(
            f"/datapipeline/api/question_set_revisions/{revision['id']}/surveys/",
            {
                'course_id': self.course.course_id,
                'idempotency_key': 'wizard-managed-survey',
                'survey_label': 'Managed reflection',
                'week_number': 4,
                'opens_at': None,
                'expires_at': None,
            },
            token=self.token,
        )
        self.assertEqual(response.status_code, 201)
        return FeedbackGPT.objects.get(public_id=response.json()['public_id'])

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

    def test_survey_creation_requires_completed_exact_revision_and_is_idempotent(self):
        draft = self.create_draft()
        revision = self.freeze_draft(draft)
        payload = {
            'course_id': self.course.course_id,
            'idempotency_key': 'wizard-create-one-survey',
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
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()['error'], 'preview_required')

        self.complete_preview(revision)
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
        self.assertFalse(public.json()['completion_certificate_enabled'])
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
