"""Unit tests for datapipeline.openai_client.

These tests are pure-function or use unittest.mock to stub the OpenAI SDK —
no real network calls, no real API key needed.
"""
import os
import unittest
from unittest import mock

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


class _FakeUsage:
    """Mimic the OpenAI Responses API usage object for tests."""
    def __init__(self, input_tokens=0, output_tokens=0, total_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens


class TestTranslateUsage(unittest.TestCase):
    """translate_usage maps Responses API native usage → Chat Completions
    shape so the frontend contract is preserved byte-for-byte."""

    def test_pydantic_style_usage_object(self):
        usage = _FakeUsage(input_tokens=10, output_tokens=20, total_tokens=30)
        result = openai_client.translate_usage(usage)
        self.assertEqual(
            result,
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    def test_dict_style_usage_payload(self):
        usage = {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}
        result = openai_client.translate_usage(usage)
        self.assertEqual(
            result,
            {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        )

    def test_none_usage_returns_empty_dict(self):
        self.assertEqual(openai_client.translate_usage(None), {})

    def test_missing_fields_default_to_zero(self):
        usage = _FakeUsage()  # all zeros
        result = openai_client.translate_usage(usage)
        self.assertEqual(
            result,
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )


class TestGetClient(unittest.TestCase):
    """get_client() is a lazy singleton. It reads oaiKey from os.environ
    (matching the env var name the existing view uses) and raises
    OpenAIConfigError when missing."""

    def setUp(self):
        # Reset the module-level cache between tests so we don't leak
        # state across test cases.
        openai_client._reset_client_for_tests()

    def tearDown(self):
        openai_client._reset_client_for_tests()

    def test_missing_env_var_raises_config_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OpenAIConfigError) as ctx:
                openai_client.get_client()
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertIn("oaiKey", ctx.exception.detail)

    def test_client_constructed_with_timeout_and_retries(self):
        fake_client_instance = mock.MagicMock()
        with mock.patch.dict(os.environ, {"oaiKey": "sk-test"}, clear=True):
            with mock.patch(
                "datapipeline.openai_client.OpenAI",
                return_value=fake_client_instance,
            ) as fake_ctor:
                result = openai_client.get_client()
                fake_ctor.assert_called_once_with(
                    api_key="sk-test",
                    timeout=60.0,
                    max_retries=2,
                )
                self.assertIs(result, fake_client_instance)

    def test_client_cached_across_calls(self):
        fake_client_instance = mock.MagicMock()
        with mock.patch.dict(os.environ, {"oaiKey": "sk-test"}, clear=True):
            with mock.patch(
                "datapipeline.openai_client.OpenAI",
                return_value=fake_client_instance,
            ) as fake_ctor:
                a = openai_client.get_client()
                b = openai_client.get_client()
                self.assertIs(a, b)
                fake_ctor.assert_called_once()  # constructed only once
