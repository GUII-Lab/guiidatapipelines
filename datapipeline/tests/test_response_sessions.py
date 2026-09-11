from __future__ import annotations

import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from unittest.mock import patch

from datapipeline.models import Course, FeedbackGPT, FeedbackMessage, ResponseSession
from datapipeline.response_sessions import (
    ResponseWriteValidationError,
    persist_feedback_messages,
)


class ResponseSessionFixtures:
    def make_course(self, suffix: str = "one") -> Course:
        return Course.objects.create(
            course_id=f"response-session-{suffix}",
            course_name=f"Response Session {suffix.title()}",
            instructor_name="Instructor",
            password="pw",
        )

    def make_survey(self, course: Course, suffix: str = "one") -> FeedbackGPT:
        return FeedbackGPT.objects.create(
            public_id=f"rs-{course.pk}-{suffix}",
            name=f"Survey {suffix.title()}",
            instructions="Ask for feedback.",
            course=course,
        )

    def message_payload(self, survey: FeedbackGPT, **overrides) -> dict:
        payload = {
            "session_id": "anonymous-session",
            "student_id": "anonymous-session",
            "sent_by": "user",
            "content": "The worked example helped.",
            "gpt_used": survey.name,
            "gpt_id": survey.pk,
            "research_consent": False,
        }
        payload.update(overrides)
        return payload


class ResponseSessionModelTests(ResponseSessionFixtures, TestCase):
    def setUp(self):
        self.course = self.make_course()
        self.survey = self.make_survey(self.course)

    def test_client_session_identifier_is_unique_within_one_survey(self):
        ResponseSession.objects.create(
            course=self.course,
            survey=self.survey,
            client_session_id="same-browser-id",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ResponseSession.objects.create(
                course=self.course,
                survey=self.survey,
                client_session_id="same-browser-id",
            )

    def test_same_client_session_identifier_can_be_used_by_another_survey(self):
        other_survey = self.make_survey(self.course, "two")

        first = ResponseSession.objects.create(
            course=self.course,
            survey=self.survey,
            client_session_id="same-browser-id",
        )
        second = ResponseSession.objects.create(
            course=self.course,
            survey=other_survey,
            client_session_id="same-browser-id",
        )

        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.public_id, second.public_id)

    def test_session_rejects_a_course_that_does_not_own_the_survey(self):
        other_course = self.make_course("two")
        session = ResponseSession(
            course=other_course,
            survey=self.survey,
            client_session_id="anonymous-session",
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Response session course must match the survey course.",
        ):
            session.full_clean()

    def test_message_sequence_is_unique_within_response_session(self):
        session = ResponseSession.objects.create(
            course=self.course,
            survey=self.survey,
            client_session_id="anonymous-session",
        )
        base = {
            "session_id": "anonymous-session",
            "student_id": "anonymous-session",
            "sent_by": "user",
            "content": "A response",
            "gpt_used": self.survey.name,
            "gpt_id": self.survey.pk,
            "response_session": session,
            "sequence": 1,
        }
        FeedbackMessage.objects.create(**base)

        with self.assertRaises(IntegrityError), transaction.atomic():
            FeedbackMessage.objects.create(**base)

    def test_legacy_message_can_keep_normalized_fields_null(self):
        message = FeedbackMessage.objects.create(
            session_id="legacy-session",
            student_id="legacy-student",
            sent_by="user",
            content="Historical response",
            gpt_used=self.survey.name,
            gpt_id=self.survey.pk,
        )

        self.assertIsNone(message.response_session_id)
        self.assertIsNone(message.sequence)


class ResponseSessionPersistenceTests(ResponseSessionFixtures, TestCase):
    def setUp(self):
        self.course = self.make_course()
        self.survey = self.make_survey(self.course)

    def test_same_survey_and_client_session_reuse_one_parent_and_allocate_sequence(self):
        messages = persist_feedback_messages([
            self.message_payload(self.survey, content="First response"),
            self.message_payload(
                self.survey,
                sent_by="assistant",
                content="First follow-up",
            ),
        ])

        self.assertEqual(ResponseSession.objects.count(), 1)
        self.assertEqual([message.sequence for message in messages], [1, 2])
        self.assertEqual(messages[0].response_session_id, messages[1].response_session_id)
        session = messages[0].response_session
        self.assertEqual(session.course_id, self.course.pk)
        self.assertEqual(session.survey_id, self.survey.pk)
        self.assertEqual(session.client_session_id, "anonymous-session")
        session.refresh_from_db()
        self.assertEqual(session.next_message_sequence, 3)

    def test_same_client_session_in_another_survey_gets_another_parent(self):
        other_survey = self.make_survey(self.course, "two")

        first = persist_feedback_messages([self.message_payload(self.survey)])[0]
        second = persist_feedback_messages([self.message_payload(other_survey)])[0]

        self.assertNotEqual(first.response_session_id, second.response_session_id)
        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 1)

    def test_batch_is_fully_validated_before_any_rows_are_written(self):
        payloads = [
            self.message_payload(self.survey),
            self.message_payload(self.survey, content=""),
        ]

        with self.assertRaises(ResponseWriteValidationError) as caught:
            persist_feedback_messages(payloads)

        self.assertEqual(caught.exception.code, "missing_fields")
        self.assertEqual(caught.exception.index, 1)
        self.assertFalse(ResponseSession.objects.exists())
        self.assertFalse(FeedbackMessage.objects.exists())

    def test_database_failure_rolls_back_session_and_counter_changes(self):
        payload = self.message_payload(self.survey)

        with patch(
            "datapipeline.response_sessions.FeedbackMessage.objects.bulk_create",
            side_effect=IntegrityError("forced persistence failure"),
        ):
            with self.assertRaises(IntegrityError):
                persist_feedback_messages([payload])

        self.assertFalse(ResponseSession.objects.exists())
        self.assertFalse(FeedbackMessage.objects.exists())


class ResponseSessionEndpointTests(ResponseSessionFixtures, TestCase):
    def setUp(self):
        self.client = Client()
        self.course = self.make_course()
        self.survey = self.make_survey(self.course)

    def post_single(self, payload: dict):
        return self.client.post(
            "/datapipeline/api/feedback_message_api/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def post_bulk(self, payloads: list[dict]):
        return self.client.post(
            "/datapipeline/api/feedback_messages_bulk_api/",
            data=json.dumps({"messages": payloads}),
            content_type="application/json",
        )

    def test_single_write_returns_and_persists_authoritative_session_metadata(self):
        response = self.post_single(self.message_payload(self.survey))

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        message = FeedbackMessage.objects.get()
        self.assertEqual(body["response_session_id"], str(message.response_session.public_id))
        self.assertEqual(body["sequence"], 1)
        self.assertEqual(message.sequence, 1)
        self.assertEqual(message.response_session.survey_id, self.survey.pk)
        self.assertEqual(message.response_session.course_id, self.course.pk)

    def test_same_client_identifier_is_isolated_between_survey_endpoints(self):
        other_survey = self.make_survey(self.course, "two")

        first = self.post_single(self.message_payload(self.survey))
        second = self.post_single(self.message_payload(other_survey))

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertNotEqual(
            first.json()["response_session_id"],
            second.json()["response_session_id"],
        )
        self.assertEqual(ResponseSession.objects.count(), 2)

    def test_bulk_write_groups_sessions_and_returns_contiguous_sequences(self):
        other_survey = self.make_survey(self.course, "two")
        payloads = [
            self.message_payload(self.survey, content="First"),
            self.message_payload(self.survey, sent_by="assistant", content="Second"),
            self.message_payload(other_survey, content="Other occurrence"),
        ]

        response = self.post_bulk(payloads)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["saved"], 3)
        self.assertEqual(len(response.json()["response_sessions"]), 2)
        first_sequences = list(
            FeedbackMessage.objects
            .filter(gpt_id=self.survey.pk)
            .order_by("sequence")
            .values_list("sequence", flat=True)
        )
        other_sequences = list(
            FeedbackMessage.objects
            .filter(gpt_id=other_survey.pk)
            .values_list("sequence", flat=True)
        )
        self.assertEqual(first_sequences, [1, 2])
        self.assertEqual(other_sequences, [1])

    def test_unknown_survey_is_rejected_without_loose_message(self):
        response = self.post_single(self.message_payload(self.survey, gpt_id=999999))

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["code"], "invalid_survey")
        self.assertFalse(ResponseSession.objects.exists())
        self.assertFalse(FeedbackMessage.objects.exists())

    def test_invalid_bulk_item_rolls_back_every_message_and_session(self):
        response = self.post_bulk([
            self.message_payload(self.survey),
            self.message_payload(self.survey, gpt_id=999999),
        ])

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["index"], 1)
        self.assertFalse(ResponseSession.objects.exists())
        self.assertFalse(FeedbackMessage.objects.exists())

    def test_bulk_rejects_a_non_object_json_body_as_client_error(self):
        response = self.client.post(
            "/datapipeline/api/feedback_messages_bulk_api/",
            data=json.dumps([self.message_payload(self.survey)]),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["code"], "invalid_body")
        self.assertFalse(ResponseSession.objects.exists())
        self.assertFalse(FeedbackMessage.objects.exists())
