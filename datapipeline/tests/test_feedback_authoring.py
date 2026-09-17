import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import TestCase

from datapipeline import models
from datapipeline.feedback_authoring import (
    apply_feedback_operations,
    run_authoring_request,
)
from datapipeline.question_sets import (
    QuestionSetError,
    create_feedback_draft,
    save_feedback_draft,
)


class FeedbackAuthoringTests(TestCase):
    def setUp(self):
        self.institution = models.Institution.objects.create(
            slug='authoring-ucsc',
            name='University of California, Santa Cruz',
        )
        user = get_user_model().objects.create_user(
            username='authoring@ucsc.edu',
            email='authoring@ucsc.edu',
            password='TemporaryPass123!',
        )
        self.account = models.InstructorAccount.objects.create(
            user=user,
            email='authoring@ucsc.edu',
            display_name='Authoring Teacher',
            must_change_password=False,
        )
        self.course = models.Course.objects.create(
            course_id='authoring-course',
            course_name='Authoring Course',
            instructor_name='Authoring Teacher',
            password=make_password(None),
            institution=self.institution,
        )
        self.draft = create_feedback_draft(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            audience='individual',
            collection_style='guided',
            source_kind='blank',
        )

    def test_ai_remove_question_applies_without_confirmation_and_validates(self):
        body = json.loads(json.dumps(self.draft.body))
        section = body['sections'][0]
        section['questions'].append({
            'id': '33333333-3333-4333-8333-333333333333',
            'short_label': 'Second question',
            'prompt': 'What should change?',
            'follow_up': {'enabled': False, 'prompt': ''},
            'response_kind': 'long_text',
        })
        changed = apply_feedback_operations(
            body,
            [{
                'op': 'remove_question',
                'question_id': section['questions'][0]['id'],
            }],
            audience='individual',
            collection_style='guided',
        )
        self.assertEqual(len(changed['sections'][0]['questions']), 1)
        self.assertEqual(
            changed['sections'][0]['questions'][0]['short_label'],
            'Second question',
        )

    def test_ai_cannot_delete_the_last_question_or_use_unknown_operations(self):
        question_id = self.draft.body['sections'][0]['questions'][0]['id']
        with self.assertRaises(QuestionSetError):
            apply_feedback_operations(
                self.draft.body,
                [{'op': 'remove_question', 'question_id': question_id}],
                audience='individual',
                collection_style='guided',
            )
        with self.assertRaises(QuestionSetError):
            apply_feedback_operations(
                self.draft.body,
                [{'op': 'read_student_responses'}],
                audience='individual',
                collection_style='guided',
            )

    @patch('datapipeline.feedback_authoring.openai_client.run_structured')
    def test_authoring_run_applies_directly_and_records_ordered_history(self, run_structured):
        run_structured.return_value = {
            'parsed': {
                'operations': [{
                    'op': 'set_title',
                    'value': 'AI-improved feedback',
                }],
                'summary': 'Updated the title',
                'rationale': 'Matched the instructor request.',
                'assistant_message': 'I updated the title and kept the questions unchanged.',
            },
            'model': 'test-model',
            'usage': {},
            'response': '{}',
        }
        run = run_authoring_request(
            draft_id=self.draft.public_id,
            actor=self.account,
            instructor_session=None,
            instruction='Make the title clearer.',
            expected_version=1,
            idempotency_key='authoring-run-1',
        )
        self.assertEqual(run.status, 'applied')
        self.assertEqual(run.applied_version.author_kind, 'ai')
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.body['title'], 'AI-improved feedback')
        messages = list(run.conversation.messages.order_by('sequence'))
        self.assertEqual([message.role for message in messages], ['instructor', 'assistant'])
        prompt_text = run_structured.call_args.kwargs['user_text']
        self.assertIn('Authoring Course', prompt_text)
        self.assertNotIn('student responses', prompt_text.casefold())

    @patch('datapipeline.feedback_authoring.openai_client.run_structured')
    def test_stale_ai_base_conflicts_instead_of_overwriting_manual_work(self, run_structured):
        run_structured.return_value = {
            'parsed': {
                'operations': [{'op': 'set_title', 'value': 'AI title'}],
                'summary': 'Updated title',
                'rationale': '',
                'assistant_message': 'Updated.',
            },
            'model': 'test-model',
            'usage': {},
            'response': '{}',
        }
        body = json.loads(json.dumps(self.draft.body))
        body['title'] = 'Manual title'
        save_feedback_draft(
            draft_id=self.draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=1,
            body=body,
            idempotency_key='manual-before-ai',
            checkpoint_reason='leave',
        )
        with self.assertRaises(QuestionSetError) as context:
            run_authoring_request(
                draft_id=self.draft.public_id,
                actor=self.account,
                instructor_session=None,
                instruction='Change the title.',
                expected_version=1,
                idempotency_key='stale-authoring-run',
            )
        self.assertEqual(context.exception.code, 'stale_draft')
