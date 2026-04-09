"""Unit tests for datapipeline.openai_client.

These tests are pure-function or use unittest.mock to stub the OpenAI SDK —
no real network calls, no real API key needed.
"""
import unittest

from datapipeline import openai_client
from datapipeline.openai_client import (
    DEFAULT_MODEL,
    OpenAIClientError,
    OpenAIConfigError,
    OpenAIRefusalError,
)


class TestModuleSurface(unittest.TestCase):
    """Sanity checks on module constants and exception hierarchy."""

    def test_default_model_is_nonempty_string(self):
        self.assertIsInstance(DEFAULT_MODEL, str)
        self.assertTrue(DEFAULT_MODEL)

    def test_exception_hierarchy(self):
        self.assertTrue(issubclass(OpenAIRefusalError, OpenAIClientError))
        self.assertTrue(issubclass(OpenAIConfigError, OpenAIClientError))
        self.assertTrue(issubclass(OpenAIClientError, Exception))

    def test_client_error_carries_detail_and_status_code(self):
        err = OpenAIClientError("boom", status_code=503)
        self.assertEqual(err.detail, "boom")
        self.assertEqual(err.status_code, 503)
        self.assertEqual(str(err), "boom")

    def test_client_error_default_status_code(self):
        err = OpenAIClientError("boom")
        self.assertEqual(err.status_code, 502)


class TestBuildResponsesInput(unittest.TestCase):
    """Verify chat_history → (instructions, input_messages) mapping.

    The existing openai_chat view (views.py:694-766) has tolerant legacy
    handling: messages may use either role/content or sent_by/content, and
    malformed entries are silently dropped. build_responses_input must
    preserve that behavior to keep the frontend contract intact."""

    def test_empty_history(self):
        instructions, input_messages = openai_client.build_responses_input(
            chat_history=[], user_text="hello",
        )
        self.assertIsNone(instructions)
        self.assertEqual(
            input_messages,
            [{"role": "user", "content": "hello"}],
        )

    def test_single_system_message_routes_to_instructions(self):
        instructions, input_messages = openai_client.build_responses_input(
            chat_history=[{"role": "system", "content": "You are a tutor."}],
            user_text="Help me with calc.",
        )
        self.assertEqual(instructions, "You are a tutor.")
        self.assertEqual(
            input_messages,
            [{"role": "user", "content": "Help me with calc."}],
        )

    def test_multiple_system_messages_newline_joined_in_order(self):
        instructions, _ = openai_client.build_responses_input(
            chat_history=[
                {"role": "system", "content": "First."},
                {"role": "system", "content": "Second."},
            ],
            user_text="hi",
        )
        self.assertEqual(instructions, "First.\n\nSecond.")

    def test_mixed_user_assistant_history(self):
        instructions, input_messages = openai_client.build_responses_input(
            chat_history=[
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ],
            user_text="q3",
        )
        self.assertEqual(instructions, "Be terse.")
        self.assertEqual(
            input_messages,
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "q3"},
            ],
        )

    def test_legacy_sent_by_student_maps_to_user(self):
        _, input_messages = openai_client.build_responses_input(
            chat_history=[{"sent_by": "student", "content": "q"}],
            user_text="followup",
        )
        self.assertEqual(
            input_messages,
            [
                {"role": "user", "content": "q"},
                {"role": "user", "content": "followup"},
            ],
        )

    def test_legacy_sent_by_gpt_maps_to_assistant(self):
        _, input_messages = openai_client.build_responses_input(
            chat_history=[{"sent_by": "gpt", "content": "a"}],
            user_text="followup",
        )
        self.assertEqual(
            input_messages,
            [
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "followup"},
            ],
        )

    def test_legacy_sent_by_ai_maps_to_assistant(self):
        _, input_messages = openai_client.build_responses_input(
            chat_history=[{"sent_by": "AI", "content": "a"}],
            user_text="followup",
        )
        self.assertEqual(
            input_messages[0],
            {"role": "assistant", "content": "a"},
        )

    def test_malformed_entry_with_no_content_dropped(self):
        _, input_messages = openai_client.build_responses_input(
            chat_history=[
                {"role": "user"},  # missing content
                {"role": "user", "content": "kept"},
                {"role": "user", "content": ""},  # empty content — drop
            ],
            user_text="final",
        )
        self.assertEqual(
            input_messages,
            [
                {"role": "user", "content": "kept"},
                {"role": "user", "content": "final"},
            ],
        )

    def test_non_dict_entries_skipped(self):
        _, input_messages = openai_client.build_responses_input(
            chat_history=["not-a-dict", None, {"role": "user", "content": "ok"}],
            user_text="end",
        )
        self.assertEqual(
            input_messages,
            [
                {"role": "user", "content": "ok"},
                {"role": "user", "content": "end"},
            ],
        )

    def test_none_chat_history_treated_as_empty(self):
        instructions, input_messages = openai_client.build_responses_input(
            chat_history=None, user_text="hi",
        )
        self.assertIsNone(instructions)
        self.assertEqual(input_messages, [{"role": "user", "content": "hi"}])

    def test_legacy_text_field_fallback(self):
        # The old view reads msg.get('content', msg.get('text', '')).
        # Preserve that fallback.
        _, input_messages = openai_client.build_responses_input(
            chat_history=[{"role": "user", "text": "legacy"}],
            user_text="end",
        )
        self.assertEqual(
            input_messages,
            [
                {"role": "user", "content": "legacy"},
                {"role": "user", "content": "end"},
            ],
        )
