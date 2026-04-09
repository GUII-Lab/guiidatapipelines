"""Unit and integration tests for datapipeline.leai_analysis.

All DB-touching tests use Django's TestCase (transactions rolled back after
each test).  Pure-function tests use unittest.TestCase for speed.
LLM calls are mocked — no real network calls.
"""
from __future__ import annotations

import json
from unittest import mock
from unittest.mock import MagicMock, patch

from django.test import TestCase

from datapipeline import leai_analysis
from datapipeline.leai_analysis import (
    QUICKTAKE_SCHEMA,
    VERIFIER_SCHEMA,
    build_chat_corpus_block,
    build_quicktake_user_text,
    build_response_corpus,
    default_chat_system_prompt,
    default_quicktake_system_prompt,
    generate_quicktake,
    parse_inline_citations,
    run_chat_turn,
    verify_claims,
)
from datapipeline.models import (
    Course,
    FeedbackGPT,
    FeedbackMessage,
    LEAIChatMessage,
    LEAIChatSession,
    LEAIQuickTake,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_course(course_id="cs101", course_name="CS 101"):
    return Course.objects.create(
        course_id=course_id,
        course_name=course_name,
        instructor_name="Prof. Test",
        password="pw",
    )


def _make_survey(course, week_number, survey_label=""):
    return FeedbackGPT.objects.create(
        name=f"Survey w{week_number}",
        instructions="Be helpful.",
        course=course,
        week_number=week_number,
        survey_label=survey_label or f"Week {week_number}",
        public_id=f"pub{course.pk}{week_number}{survey_label[:2]}",
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


# ---------------------------------------------------------------------------
# BuildResponseCorpusTest
# ---------------------------------------------------------------------------

class BuildResponseCorpusTest(TestCase):
    """Tests for build_response_corpus()."""

    def setUp(self):
        self.course = _make_course()
        self.w1 = _make_survey(self.course, week_number=1)
        self.w2 = _make_survey(self.course, week_number=2, survey_label="W2")

        # 3 sessions: 2 in week 1, 1 in week 2
        _make_msg(self.w1, "sess-a", "Lecture was too fast")
        _make_msg(self.w1, "sess-b", "Good examples")
        _make_msg(self.w2, "sess-c", "More practice problems please")

    def test_course_scope_returns_all_student_messages(self):
        corpus = build_response_corpus(self.course, scope_kind="course")
        self.assertEqual(len(corpus), 3)

    def test_week_scope_filters_to_one_week(self):
        corpus = build_response_corpus(
            self.course, scope_kind="week", scope_week_number=1
        )
        self.assertEqual(len(corpus), 2)
        for entry in corpus:
            self.assertEqual(entry["week_number"], 1)

    def test_responses_are_ordered_by_week_then_session(self):
        corpus = build_response_corpus(self.course, scope_kind="course")
        weeks = [e["week_number"] for e in corpus]
        # Week 1 entries come before week 2
        self.assertEqual(sorted(weeks), weeks)

    def test_ai_messages_are_excluded(self):
        # Add an AI message to week 1 survey
        _make_msg(self.w1, "sess-a", "AI response here", sent_by="ai")
        corpus = build_response_corpus(self.course, scope_kind="course")
        # Count should still be 3 (same as setUp — AI message excluded)
        self.assertEqual(len(corpus), 3)

    def test_student_messages_concatenated_per_session(self):
        # Add a second message to sess-a in week 1
        _make_msg(self.w1, "sess-a", "Also slides were unclear")
        corpus = build_response_corpus(self.course, scope_kind="course")
        # Find sess-a entry
        sess_a = next(e for e in corpus if e["session_id"] == "sess-a")
        self.assertIn(" | ", sess_a["text"])
        self.assertIn("Lecture was too fast", sess_a["text"])
        self.assertIn("Also slides were unclear", sess_a["text"])
        # Still 3 unique sessions
        self.assertEqual(len(corpus), 3)


# ---------------------------------------------------------------------------
# ParseInlineCitationsTest
# ---------------------------------------------------------------------------

class ParseInlineCitationsTest(TestCase):
    """Tests for parse_inline_citations()."""

    def test_single_citation(self):
        cleaned, cited = parse_inline_citations("Students struggled [R17] with pace.")
        self.assertIn("[1]", cleaned)
        self.assertNotIn("[R17]", cleaned)
        self.assertEqual(cited, ["R17"])

    def test_multiple_citations(self):
        text = "Theme A [R17] and also [R42] and finally [R71]."
        cleaned, cited = parse_inline_citations(text)
        self.assertIn("[1]", cleaned)
        self.assertIn("[2]", cleaned)
        self.assertIn("[3]", cleaned)
        self.assertNotIn("[R", cleaned)
        self.assertEqual(cited, ["R17", "R42", "R71"])

    def test_no_citations(self):
        text = "No citations here at all."
        cleaned, cited = parse_inline_citations(text)
        self.assertEqual(cleaned, text)
        self.assertEqual(cited, [])

    def test_duplicate_citation_gets_separate_pills(self):
        # [R17] appears twice → two separate pill indices [1] and [2]
        text = "First mention [R17] and again [R17]."
        cleaned, cited = parse_inline_citations(text)
        self.assertIn("[1]", cleaned)
        self.assertIn("[2]", cleaned)
        self.assertNotIn("[R17]", cleaned)
        # cited list de-duplicates: R17 listed once
        self.assertEqual(cited, ["R17"])


# ---------------------------------------------------------------------------
# PromptsAndSchemasTest
# ---------------------------------------------------------------------------

class PromptsAndSchemasTest(TestCase):
    """Tests for prompt functions, schema constants, and user-text builders."""

    def test_default_quicktake_prompt_is_nonempty(self):
        prompt = default_quicktake_system_prompt()
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 0)
        self.assertIn("response IDs", prompt)

    def test_default_chat_prompt_is_nonempty(self):
        prompt = default_chat_system_prompt()
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 0)
        self.assertIn("LEAI", prompt)

    def test_quicktake_schema_is_valid_json_schema(self):
        self.assertIsInstance(QUICKTAKE_SCHEMA, dict)
        self.assertIn("bullets", QUICKTAKE_SCHEMA.get("properties", {}))

    def test_verifier_schema_is_valid_json_schema(self):
        self.assertIsInstance(VERIFIER_SCHEMA, dict)
        self.assertIn("results", VERIFIER_SCHEMA.get("properties", {}))

    def test_build_quicktake_user_text_includes_all_responses(self):
        corpus = [
            {"rid": "R1", "survey_id": 1, "session_id": "s1",
             "week_number": 1, "text": "Lectures are great"},
            {"rid": "R2", "survey_id": 1, "session_id": "s2",
             "week_number": 1, "text": "Need more examples"},
        ]
        user_text = build_quicktake_user_text(
            course_name="Test Course", corpus=corpus, scope_label="Week 1"
        )
        self.assertIn("[R1]", user_text)
        self.assertIn("[R2]", user_text)
        self.assertIn("Lectures are great", user_text)
        self.assertIn("Need more examples", user_text)


# ---------------------------------------------------------------------------
# VerifyClaimsTest
# ---------------------------------------------------------------------------

class VerifyClaimsTest(TestCase):
    """Tests for verify_claims() — mocks openai_client.run_structured."""

    def _mock_structured(self, parsed):
        return {
            "response": json.dumps(parsed),
            "parsed": parsed,
            "usage": {},
            "model": "gpt-5.1",
        }

    def test_verify_claims_returns_verdicts(self):
        corpus = [
            {"rid": "R1", "text": "Pace was too fast", "survey_id": 1,
             "session_id": "s1", "week_number": 1},
        ]
        bullets = [{"text": "Students found the pace too fast.", "cited_ids": ["R1"]}]

        expected_results = [
            {"bullet_index": 0, "source_id": "R1", "verdict": "supported"}
        ]
        mock_return = self._mock_structured({"results": expected_results})

        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value=mock_return,
        ):
            results = verify_claims(corpus=corpus, bullets=bullets)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["verdict"], "supported")
        self.assertEqual(results[0]["source_id"], "R1")


# ---------------------------------------------------------------------------
# GenerateQuickTakeTest
# ---------------------------------------------------------------------------

class GenerateQuickTakeTest(TestCase):
    """Tests for generate_quicktake() — mocks LLM calls."""

    def setUp(self):
        self.course = _make_course("cse110", "CSE 110")
        self.survey = _make_survey(self.course, week_number=1)
        # Create >=20 sessions to satisfy the minimum threshold
        for i in range(25):
            _make_msg(self.survey, f"sess-{i}", f"Response from student {i}")

    def _mock_structured_result(self):
        bullets = [
            {"text": "Many students liked the labs [R1].", "cited_ids": ["R1"]},
        ]
        parsed = {"bullets": bullets}
        return {
            "response": json.dumps(parsed),
            "parsed": parsed,
            "usage": {},
            "model": "gpt-5.1",
        }

    def test_generate_quicktake_creates_row(self):
        with patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value=self._mock_structured_result(),
        ):
            qt = generate_quicktake(
                course=self.course,
                scope_key="course",
                scope_kind="course",
            )

        self.assertIsInstance(qt, LEAIQuickTake)
        self.assertEqual(qt.course, self.course)
        self.assertEqual(qt.scope_key, "course")
        self.assertIsInstance(qt.bullets, list)
        self.assertGreater(len(qt.bullets), 0)
        self.assertIsInstance(qt.model_name, str)

    def test_generate_quicktake_raises_on_insufficient_data(self):
        # Create a fresh course with no messages
        empty_course = _make_course("empty-c", "Empty Course")
        survey = _make_survey(empty_course, week_number=1, survey_label="EW1")
        # Add only 5 messages — below the 20-response threshold
        for i in range(5):
            _make_msg(survey, f"e-sess-{i}", f"Short response {i}")

        with self.assertRaises(ValueError) as ctx:
            generate_quicktake(
                course=empty_course,
                scope_key="course",
                scope_kind="course",
            )
        self.assertIn("20", str(ctx.exception))


# ---------------------------------------------------------------------------
# RunChatTurnTest
# ---------------------------------------------------------------------------

class RunChatTurnTest(TestCase):
    """Tests for run_chat_turn() — mocks openai_client.run_chat."""

    def setUp(self):
        self.course = _make_course("chat-c", "Chat Course")
        self.survey = _make_survey(self.course, week_number=1)
        _make_msg(self.survey, "s1", "I enjoy the readings")

        self.session = LEAIChatSession.objects.create(
            course=self.course,
            title="Test session",
            scope_kind="course",
        )

    def _mock_run_chat(self, response_text="Here is the analysis [R1]."):
        return {
            "response": response_text,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "model": "gpt-5.1",
        }

    def test_run_chat_turn_saves_both_messages(self):
        with patch(
            "datapipeline.leai_analysis.openai_client.run_chat",
            return_value=self._mock_run_chat("Students found it helpful [R1]."),
        ), patch(
            "datapipeline.leai_analysis.openai_client.run_structured",
            return_value={
                "response": '{"results":[]}',
                "parsed": {"results": []},
                "usage": {},
                "model": "gpt-5.1",
            },
        ):
            assistant_msg = run_chat_turn(
                session=self.session,
                user_text="What are the main themes?",
            )

        # Both messages saved
        messages = list(self.session.messages.order_by("created_at"))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].text, "What are the main themes?")
        self.assertEqual(messages[1].role, "assistant")
        # Citations should have been parsed ([R1] → [1])
        self.assertNotIn("[R1]", messages[1].text)
        self.assertIn("[1]", messages[1].text)
        self.assertIn("R1", messages[1].cited)

    def test_run_chat_turn_rolls_back_on_llm_error(self):
        from datapipeline.openai_client import OpenAIClientError

        with patch(
            "datapipeline.leai_analysis.openai_client.run_chat",
            side_effect=OpenAIClientError("LLM failure", status_code=502),
        ):
            with self.assertRaises(OpenAIClientError):
                run_chat_turn(
                    session=self.session,
                    user_text="Trigger a failure",
                )

        # No messages should have been saved (transaction rolled back)
        count = LEAIChatMessage.objects.filter(session=self.session).count()
        self.assertEqual(count, 0)
