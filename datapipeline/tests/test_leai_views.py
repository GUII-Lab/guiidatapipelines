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

    def test_create_session_with_seed_assistant_message(self):
        payload = {
            "course_id": self.course.course_id,
            "scope": {"kind": "course"},
            "seed_assistant_message": {
                "text": "- Students liked the group work [1][2]",
                "cited": [
                    {"rid": "R1", "pill_index": 1, "verdict": "verified"},
                    {"rid": "R3", "pill_index": 2, "verdict": None},
                ],
            },
        }
        resp = _post(self.client, self.list_url, payload)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(len(data["messages"]), 1)
        msg = data["messages"][0]
        self.assertEqual(msg["role"], "assistant")
        self.assertIn("[1][2]", msg["text"])
        self.assertEqual(len(msg["cited"]), 2)
        self.assertEqual(msg["cited"][0]["rid"], "R1")
        self.assertEqual(msg["cited"][1]["pill_index"], 2)

    def test_create_session_ignores_empty_seed_assistant(self):
        payload = {
            "course_id": self.course.course_id,
            "scope": {"kind": "course"},
            "seed_assistant_message": {"text": "", "cited": []},
        }
        resp = _post(self.client, self.list_url, payload)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(resp.json()["messages"]), 0)

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

    def test_get_session_includes_corpus_keyed_by_rid(self):
        # The session-detail endpoint must ship the same corpus the chat
        # turn used, so the frontend can resolve citation popovers without
        # rebuilding (and risking a scope mismatch).
        survey = _make_survey(self.course, week_number=1)
        _make_msg(survey, session_id="sa", content="First reflection")
        _make_msg(survey, session_id="sb", content="Second reflection")

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertIn("corpus", data)
        rids = [e["rid"] for e in data["corpus"]]
        # R1, R2 in deterministic backend order
        self.assertEqual(rids, ["R1", "R2"])
        # Each entry carries the metadata the frontend needs for the popover
        first = data["corpus"][0]
        for key in ("rid", "text", "session_id", "week_number"):
            self.assertIn(key, first)

    def test_get_session_corpus_respects_week_scope(self):
        # With a week-scoped session, the corpus only contains that week's
        # sessions — and the rid numbering reflects that narrower set.
        wk1 = _make_survey(self.course, week_number=1, suffix="a")
        wk2 = _make_survey(self.course, week_number=2, suffix="b")
        _make_msg(wk1, "sa", "wk1 only")
        _make_msg(wk2, "sb", "wk2 only")

        self.session.scope_kind = "week"
        self.session.scope_week_number = 2
        self.session.save()

        resp = self.client.get(self.url)
        data = resp.json()
        rids = [e["rid"] for e in data["corpus"]]
        texts = [e["text"] for e in data["corpus"]]
        self.assertEqual(rids, ["R1"])
        self.assertEqual(texts, ["wk2 only"])

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

    def _run_worker_inline(self):
        """Replace threading.Thread inside leai_analysis so the worker
        executes on the request thread — tests stay synchronous while the
        production view still spawns a real daemon thread."""
        class _InlineThread:
            def __init__(self, target=None, args=(), name=None, daemon=None,
                         **kwargs):
                self._target = target
                self._args = args

            def start(self):
                self._target(*self._args)

        return patch(
            "datapipeline.leai_analysis.threading.Thread", _InlineThread,
        )

    def test_successful_turn_returns_pending_then_ready(self):
        # Phase 6: chat turns go through run_structured for both the chat
        # answer and the verifier. side_effect feeds them in order.
        chat_result = {
            "response": '{"answer":"Great insights [R1][R2].","quotes":[]}',
            "parsed": {
                "answer": "Great insights [R1][R2].",
                "quotes": [
                    {"rid": "R1", "text": "Feedback 0"},
                    {"rid": "R2", "text": "Feedback 1"},
                ],
            },
            "usage": {},
        }
        verifier_result = {
            "parsed": {"results": [{"bullet_index": 0, "source_id": "R1",
                                    "verdict": "supported"}]},
            "usage": {},
        }
        with patch.object(openai_client, "run_structured",
                          side_effect=[chat_result, verifier_result]), \
                self._run_worker_inline():
            resp = _post(self.client, self.url,
                         {"user_text": "What are the main themes?"})

        # 202 with pending placeholder + user message saved synchronously.
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertIn("message", data)
        self.assertIn("user_message", data)
        self.assertEqual(data["message"]["role"], "assistant")
        self.assertEqual(data["user_message"]["role"], "user")
        self.assertIn("session_updated_at", data)

        # Worker ran inline, so the DB now has the ready assistant message.
        msg = LEAIChatMessage.objects.get(pk=data["message"]["id"])
        self.assertEqual(msg.status, LEAIChatMessage.STATUS_READY)
        # Citations replaced: [R1] -> [1], [R2] -> [2]
        self.assertIn("[1]", msg.text)
        rid_to_quote = {c["rid"]: c["quote_text"] for c in msg.cited}
        self.assertEqual(rid_to_quote["R1"], "Feedback 0")

    def test_empty_user_text_returns_400(self):
        resp = _post(self.client, self.url, {"user_text": ""})
        self.assertEqual(resp.status_code, 400)

    def test_missing_user_text_returns_400(self):
        resp = _post(self.client, self.url, {})
        self.assertEqual(resp.status_code, 400)

    def test_llm_refusal_marks_message_failed(self):
        with patch.object(openai_client, "run_structured",
                          side_effect=openai_client.OpenAIRefusalError("refused")), \
                self._run_worker_inline():
            resp = _post(self.client, self.url, {"user_text": "Tell me something"})

        self.assertEqual(resp.status_code, 202)
        msg = LEAIChatMessage.objects.get(pk=resp.json()["message"]["id"])
        self.assertEqual(msg.status, LEAIChatMessage.STATUS_FAILED)
        self.assertTrue(msg.error)

    def test_llm_error_marks_message_failed(self):
        with patch.object(openai_client, "run_structured",
                          side_effect=openai_client.OpenAIClientError(
                              "network error", 502)), \
                self._run_worker_inline():
            resp = _post(self.client, self.url, {"user_text": "Tell me something"})

        self.assertEqual(resp.status_code, 202)
        msg = LEAIChatMessage.objects.get(pk=resp.json()["message"]["id"])
        self.assertEqual(msg.status, LEAIChatMessage.STATUS_FAILED)
        self.assertIn("network error", msg.error)

    def test_session_not_found_returns_404(self):
        import uuid
        url = f"/datapipeline/api/leai_chat_sessions/{uuid.uuid4()}/turn/"
        resp = _post(self.client, url, {"user_text": "Hello"})
        self.assertEqual(resp.status_code, 404)

    def test_message_detail_returns_status(self):
        # Create a pending assistant message directly, then GET it.
        msg = LEAIChatMessage.objects.create(
            session=self.session, role="assistant", text="",
            cited=[], status=LEAIChatMessage.STATUS_PENDING,
        )
        url = (f"/datapipeline/api/leai_chat_sessions/{self.session.pk}"
               f"/messages/{msg.pk}/")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], msg.pk)
        self.assertEqual(data["status"], LEAIChatMessage.STATUS_PENDING)
        self.assertEqual(data["role"], "assistant")

    def test_message_detail_404_for_unknown_message(self):
        url = (f"/datapipeline/api/leai_chat_sessions/{self.session.pk}"
               f"/messages/999999/")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_message_detail_recovers_stale_pending(self):
        # A pending message past the stale threshold gets reported as failed.
        from datetime import timedelta
        from django.utils import timezone
        from datapipeline import leai_analysis
        msg = LEAIChatMessage.objects.create(
            session=self.session, role="assistant", text="",
            cited=[], status=LEAIChatMessage.STATUS_PENDING,
            job_started_at=timezone.now() - timedelta(
                seconds=leai_analysis.CHAT_TURN_JOB_STALE_SECONDS + 60,
            ),
        )
        url = (f"/datapipeline/api/leai_chat_sessions/{self.session.pk}"
               f"/messages/{msg.pk}/")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], LEAIChatMessage.STATUS_FAILED)
        self.assertTrue(data["error"])


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
