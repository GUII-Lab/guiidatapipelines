"""Tests for LEAI REST endpoints (views).

Covers:
  - leai_chat_sessions_list (GET + POST)
  - leai_chat_session_detail (GET + PATCH + DELETE)
  - leai_chat_session_turn (POST)
  - leai_quicktake_fetch_or_delete (GET + DELETE)
  - leai_quicktake_generate (POST)
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, Client
from django.urls import reverse

from datapipeline import openai_client
from datapipeline.models import (
    Course,
    FeedbackGPT,
    FeedbackMessage,
    LEAIChatMessage,
    LEAIChatSession,
    LEAIQuickTake,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_course(course_id="cs101", course_name="CS 101"):
    return Course.objects.create(
        course_id=course_id,
        course_name=course_name,
        instructor_name="Prof. Test",
        password="pw",
    )


def _make_survey(course, week_number, suffix=""):
    return FeedbackGPT.objects.create(
        name=f"Survey w{week_number}{suffix}",
        instructions="Be helpful.",
        course=course,
        week_number=week_number,
        survey_label=f"Week {week_number}{suffix}",
        public_id=f"pub{course.pk}{week_number}{suffix}",
    )


def _make_msg(survey, session_id, content, sent_by="user-message"):
    return FeedbackMessage.objects.create(
        session_id=session_id,
        student_id="anon",
        sent_by=sent_by,
        content=content,
        gpt_used="test",
        gpt_id=survey.pk,
    )


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def _patch(client, url, payload):
    return client.patch(url, data=json.dumps(payload), content_type="application/json")


# ---------------------------------------------------------------------------
# ChatSessionsListTest
# ---------------------------------------------------------------------------

class ChatSessionsListTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.course = _make_course()
        self.list_url = "/datapipeline/api/leai_chat_sessions/"

    def test_list_sessions(self):
        LEAIChatSession.objects.create(course=self.course, title="S1")
        LEAIChatSession.objects.create(course=self.course, title="S2")
        resp = self.client.get(self.list_url, {"course_id": self.course.course_id})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("sessions", data)
        self.assertEqual(len(data["sessions"]), 2)

    def test_list_sessions_missing_course_id(self):
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, 400)

    def test_list_sessions_unknown_course(self):
        resp = self.client.get(self.list_url, {"course_id": "no-such-course"})
        self.assertEqual(resp.status_code, 404)

    def test_create_session(self):
        payload = {
            "course_id": self.course.course_id,
            "title": "My Session",
            "scope": {"kind": "course"},
        }
        resp = _post(self.client, self.list_url, payload)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["title"], "My Session")
        self.assertIn("messages", data)
        self.assertEqual(len(data["messages"]), 0)

    def test_create_session_with_seed_message(self):
        payload = {
            "course_id": self.course.course_id,
            "scope": {"kind": "course"},
            "seed_system_message": "You are a custom assistant.",
        }
        resp = _post(self.client, self.list_url, payload)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["role"], "system")
        self.assertEqual(data["messages"][0]["text"], "You are a custom assistant.")

    def test_create_session_missing_course_id(self):
        resp = _post(self.client, self.list_url, {"scope": {"kind": "course"}})
        self.assertEqual(resp.status_code, 400)

    def test_create_session_unknown_course(self):
        payload = {"course_id": "ghost", "scope": {"kind": "course"}}
        resp = _post(self.client, self.list_url, payload)
        self.assertEqual(resp.status_code, 404)

    def test_method_not_allowed(self):
        resp = self.client.put(self.list_url, data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# ChatSessionDetailTest
# ---------------------------------------------------------------------------

class ChatSessionDetailTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.course = _make_course()
        self.session = LEAIChatSession.objects.create(
            course=self.course, title="Detail Test"
        )
        self.url = f"/datapipeline/api/leai_chat_sessions/{self.session.pk}/"

    def test_get_session_with_messages(self):
        LEAIChatMessage.objects.create(session=self.session, role="user", text="Hello", cited=[])
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["title"], "Detail Test")
        self.assertIn("messages", data)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["role"], "user")

    def test_get_session_404(self):
        import uuid
        resp = self.client.get(f"/datapipeline/api/leai_chat_sessions/{uuid.uuid4()}/")
        self.assertEqual(resp.status_code, 404)

    def test_patch_title(self):
        resp = _patch(self.client, self.url, {"title": "Renamed"})
        self.assertEqual(resp.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.title, "Renamed")

    def test_patch_scope(self):
        resp = _patch(self.client, self.url, {"scope": {"kind": "week", "week_number": 3}})
        self.assertEqual(resp.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.scope_kind, "week")
        self.assertEqual(self.session.scope_week_number, 3)

    def test_delete_session(self):
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(LEAIChatSession.objects.filter(pk=self.session.pk).exists())

    def test_delete_cascades_messages(self):
        LEAIChatMessage.objects.create(session=self.session, role="user", text="x", cited=[])
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(LEAIChatMessage.objects.filter(session=self.session).exists())

    def test_method_not_allowed(self):
        resp = _post(self.client, self.url, {})
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# ChatSessionTurnTest
# ---------------------------------------------------------------------------

class ChatSessionTurnTest(TestCase):
    """Successful turn test with mocked LLM calls."""

    def setUp(self):
        self.client = Client()
        self.course = _make_course()
        # Create enough survey data so corpus has >= 1 entry
        survey = _make_survey(self.course, week_number=1)
        for i in range(6):
            _make_msg(survey, session_id=f"sess-{i}", content=f"Feedback {i}")
        self.session = LEAIChatSession.objects.create(
            course=self.course,
            title="Turn Test",
            scope_kind="course",
        )
        self.url = f"/datapipeline/api/leai_chat_sessions/{self.session.pk}/turn/"

    def test_successful_turn_returns_assistant_message(self):
        mock_chat_result = {"response": "Great insights [R1][R2].", "usage": {}}
        mock_struct_result = {
            "parsed": {"results": [{"bullet_index": 0, "source_id": "R1", "verdict": "supported"}]},
            "usage": {},
        }
        with patch.object(openai_client, "run_chat", return_value=mock_chat_result) as m_chat, \
             patch.object(openai_client, "run_structured", return_value=mock_struct_result):
            resp = _post(self.client, self.url, {"user_text": "What are the main themes?"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("message", data)
        self.assertEqual(data["message"]["role"], "assistant")
        # Citations replaced: [R1] -> [1], [R2] -> [2]
        self.assertIn("[1]", data["message"]["text"])
        self.assertIn("session_updated_at", data)

    def test_empty_user_text_returns_400(self):
        resp = _post(self.client, self.url, {"user_text": ""})
        self.assertEqual(resp.status_code, 400)

    def test_missing_user_text_returns_400(self):
        resp = _post(self.client, self.url, {})
        self.assertEqual(resp.status_code, 400)

    def test_llm_refusal_returns_422(self):
        with patch.object(openai_client, "run_chat",
                          side_effect=openai_client.OpenAIRefusalError("refused")), \
             patch.object(openai_client, "run_structured", return_value={"parsed": {"results": []}, "usage": {}}):
            resp = _post(self.client, self.url, {"user_text": "Tell me something"})
        self.assertEqual(resp.status_code, 422)

    def test_llm_error_returns_502(self):
        with patch.object(openai_client, "run_chat",
                          side_effect=openai_client.OpenAIClientError("network error", 502)), \
             patch.object(openai_client, "run_structured", return_value={"parsed": {"results": []}, "usage": {}}):
            resp = _post(self.client, self.url, {"user_text": "Tell me something"})
        self.assertEqual(resp.status_code, 502)

    def test_session_not_found_returns_404(self):
        import uuid
        url = f"/datapipeline/api/leai_chat_sessions/{uuid.uuid4()}/turn/"
        resp = _post(self.client, url, {"user_text": "Hello"})
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# QuickTakeTest
# ---------------------------------------------------------------------------

class QuickTakeTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.course = _make_course()
        self.fetch_url = "/datapipeline/api/leai_quicktake/"
        self.generate_url = "/datapipeline/api/leai_quicktake/generate/"

    def test_get_nonexistent_returns_404(self):
        resp = self.client.get(self.fetch_url, {
            "course_id": self.course.course_id,
            "scope_key": "week:1",
        })
        self.assertEqual(resp.status_code, 404)

    def test_get_existing_returns_200(self):
        qt = LEAIQuickTake.objects.create(
            course=self.course,
            scope_key="week:1",
            bullets=[{"text": "Good feedback.", "cited_ids": ["R1"]}],
            verification=[],
            system_prompt="sys",
            user_text="user",
            model_name="gpt-4o",
        )
        resp = self.client.get(self.fetch_url, {
            "course_id": self.course.course_id,
            "scope_key": "week:1",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["scope_key"], "week:1")
        self.assertEqual(len(data["bullets"]), 1)

    def test_delete_clears_cache(self):
        LEAIQuickTake.objects.create(
            course=self.course,
            scope_key="week:2",
            bullets=[],
            verification=[],
            system_prompt="sys",
            user_text="user",
            model_name="gpt-4o",
        )
        resp = self.client.delete(self.fetch_url + "?course_id=" + self.course.course_id + "&scope_key=week:2")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(
            LEAIQuickTake.objects.filter(course=self.course, scope_key="week:2").exists()
        )

    def test_get_missing_params_returns_400(self):
        resp = self.client.get(self.fetch_url, {"course_id": self.course.course_id})
        self.assertEqual(resp.status_code, 400)

    def test_generate_enqueues_job_and_returns_202(self):
        # Generate is async: POST enqueues a worker thread and returns 202
        # with the initial pending row. Clients poll GET for completion.
        survey = _make_survey(self.course, week_number=1)
        for i in range(20):
            _make_msg(survey, session_id=f"gen-{i}", content=f"Response {i}")

        from django.utils import timezone as tz
        now = tz.now()
        mock_qt = LEAIQuickTake(
            course=self.course,
            scope_key="course:all",
            bullets=[],
            verification=[],
            system_prompt="",
            user_text="",
            model_name="",
            status=LEAIQuickTake.STATUS_PENDING,
            error="",
            job_started_at=now,
        )
        mock_qt.pk = 999
        mock_qt.created_at = now
        mock_qt.updated_at = now

        from datapipeline import leai_analysis
        with patch.object(
            leai_analysis, "start_quicktake_job", return_value=(mock_qt, True)
        ) as m_start:
            resp = _post(self.client, self.generate_url, {
                "course_id": self.course.course_id,
                "scope_key": "course:all",
                "scope": {"kind": "course"},
            })

        self.assertEqual(resp.status_code, 202)
        m_start.assert_called_once()
        data = resp.json()
        self.assertEqual(data["scope_key"], "course:all")
        self.assertEqual(data["status"], LEAIQuickTake.STATUS_PENDING)

    def test_generate_missing_params_returns_400(self):
        resp = _post(self.client, self.generate_url, {"course_id": self.course.course_id})
        self.assertEqual(resp.status_code, 400)

    def test_generate_unknown_course_returns_404(self):
        resp = _post(self.client, self.generate_url, {
            "course_id": "ghost",
            "scope_key": "course:all",
            "scope": {"kind": "course"},
        })
        self.assertEqual(resp.status_code, 404)
