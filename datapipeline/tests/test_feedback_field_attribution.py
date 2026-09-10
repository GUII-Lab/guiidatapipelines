from __future__ import annotations

import json

from django.test import Client, TestCase

from datapipeline.models import Course, FeedbackGPT, FeedbackMessage


class FeedbackFieldAttributionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.course = Course.objects.create(
            course_id="cmpm80h-wi27-test",
            course_name="CMPM 80H Winter 2027 Test",
            instructor_name="Instructor",
            password="pw",
        )
        self.survey = FeedbackGPT.objects.create(
            name="Field-by-field test",
            instructions="Ask one field at a time.",
            course=self.course,
            week_number=1,
            survey_label="Week 1",
            public_id="field-attr-test",
            mode="form",
        )
        self.attribution = {
            "form_schema_id": "collab-ai-metacognition",
            "form_schema_version": "1.0.0",
            "form_section_id": "understanding",
            "form_field_id": "mental-model",
            "form_field_label": "How do you think the AI produced its response?",
            "form_response_phase": "primary",
        }

    def _message_payload(self, **overrides):
        payload = {
            "session_id": "field-session",
            "student_id": "anon",
            "sent_by": "user-message",
            "content": "I checked its response against my notes.",
            "gpt_used": "field-test",
            "gpt_id": self.survey.pk,
            **self.attribution,
        }
        payload.update(overrides)
        return payload

    def test_single_create_persists_field_attribution(self):
        response = self.client.post(
            "/datapipeline/api/feedback_message_api/",
            data=json.dumps(self._message_payload()),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        message = FeedbackMessage.objects.get(session_id="field-session")
        for field_name, expected in self.attribution.items():
            self.assertEqual(getattr(message, field_name), expected)

    def test_bulk_create_persists_optional_field_attribution(self):
        attributed = self._message_payload(session_id="bulk-session")
        historical = self._message_payload(
            session_id="bulk-session",
            content="Historical answer",
        )
        for field_name in self.attribution:
            historical.pop(field_name)

        response = self.client.post(
            "/datapipeline/api/feedback_messages_bulk_api/",
            data=json.dumps({"messages": [attributed, historical]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        messages = list(FeedbackMessage.objects.filter(session_id="bulk-session").order_by("id"))
        self.assertEqual(messages[0].form_field_id, "mental-model")
        self.assertIsNone(messages[1].form_field_id)
        self.assertIsNone(messages[1].form_response_phase)

    def test_resume_and_researcher_lists_return_field_attribution(self):
        response = self.client.post(
            "/datapipeline/api/feedback_message_api/",
            data=json.dumps(self._message_payload()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        resume = self.client.post(
            "/datapipeline/api/feedback_session_resume/",
            data=json.dumps({"gpt_id": self.survey.pk, "session_id": "field-session"}),
            content_type="application/json",
        )
        resumed_message = resume.json()["messages"][0]
        for field_name, expected in self.attribution.items():
            self.assertEqual(resumed_message[field_name], expected)

        by_gpt = self.client.get(
            "/datapipeline/api/feedback_messages_by_gpt/",
            {"gpt_id": self.survey.pk},
        ).json()["sessions"]["field-session"][0]
        by_course = self.client.get(
            "/datapipeline/api/feedback_messages_by_course/",
            {"course_id": self.course.course_id},
        ).json()[0]["sessions"]["field-session"][0]
        for serialized in (by_gpt, by_course):
            for field_name, expected in self.attribution.items():
                self.assertEqual(serialized[field_name], expected)
