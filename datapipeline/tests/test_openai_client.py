"""Unit tests for datapipeline.openai_client.

These tests are pure-function or use unittest.mock to stub the OpenAI SDK —
no real network calls, no real API key needed.
"""
import os
import unittest
from unittest import mock

import openai  # for error-class references in tests

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


class _FakeResponse:
    """Mimic the object client.responses.create() returns."""
    def __init__(self, output_text="", usage=None, model="gpt-5.1"):
        self.output_text = output_text
        self.usage = usage
        self.model = model


def _fake_response(text="hi", model="gpt-5.1"):
    return _FakeResponse(
        output_text=text,
        usage=_FakeUsage(input_tokens=3, output_tokens=2, total_tokens=5),
        model=model,
    )


class TestRunChat(unittest.TestCase):
    """run_chat wraps client.responses.create with error handling and
    usage translation. The SDK is mocked — no real network calls."""

    def setUp(self):
        openai_client._reset_client_for_tests()

    def tearDown(self):
        openai_client._reset_client_for_tests()

    def _make_client_mock(self, create_return_value=None, create_side_effect=None):
        """Return a patched OpenAI() constructor whose .responses.create is
        configurable."""
        fake_client = mock.MagicMock()
        if create_side_effect is not None:
            fake_client.responses.create.side_effect = create_side_effect
        else:
            fake_client.responses.create.return_value = (
                create_return_value if create_return_value is not None else _fake_response()
            )
        env_patch = mock.patch.dict(os.environ, {"oaiKey": "sk-test"}, clear=True)
        ctor_patch = mock.patch(
            "datapipeline.openai_client.OpenAI",
            return_value=fake_client,
        )
        return fake_client, env_patch, ctor_patch

    def test_happy_path_returns_expected_dict_shape(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            create_return_value=_fake_response(text="Hello, student.")
        )
        with env_patch, ctor_patch:
            result = openai_client.run_chat(
                chat_history=[],
                user_text="Hi",
            )
        self.assertEqual(
            result,
            {
                "response": "Hello, student.",
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
                "model": "gpt-5.1",
            },
        )

    def test_default_model_used_when_caller_omits_model(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock()
        with env_patch, ctor_patch:
            openai_client.run_chat(chat_history=[], user_text="Hi")
        _, kwargs = fake_client.responses.create.call_args
        self.assertEqual(kwargs["model"], DEFAULT_MODEL)

    def test_custom_model_passes_through(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock()
        with env_patch, ctor_patch:
            openai_client.run_chat(
                chat_history=[],
                user_text="Hi",
                model="gpt-4o",
            )
        _, kwargs = fake_client.responses.create.call_args
        self.assertEqual(kwargs["model"], "gpt-4o")

    def test_instructions_omitted_when_no_system_message(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock()
        with env_patch, ctor_patch:
            openai_client.run_chat(chat_history=[], user_text="Hi")
        _, kwargs = fake_client.responses.create.call_args
        self.assertNotIn("instructions", kwargs)

    def test_instructions_passed_when_system_message_present(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock()
        with env_patch, ctor_patch:
            openai_client.run_chat(
                chat_history=[{"role": "system", "content": "Be terse."}],
                user_text="Hi",
            )
        _, kwargs = fake_client.responses.create.call_args
        self.assertEqual(kwargs["instructions"], "Be terse.")

    def test_input_messages_built_from_history_plus_user_text(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock()
        with env_patch, ctor_patch:
            openai_client.run_chat(
                chat_history=[
                    {"role": "user", "content": "earlier q"},
                    {"role": "assistant", "content": "earlier a"},
                ],
                user_text="latest",
            )
        _, kwargs = fake_client.responses.create.call_args
        self.assertEqual(
            kwargs["input"],
            [
                {"role": "user", "content": "earlier q"},
                {"role": "assistant", "content": "earlier a"},
                {"role": "user", "content": "latest"},
            ],
        )

    def test_rate_limit_error_mapped_to_429(self):
        rate_limit = openai.RateLimitError(
            message="slow down",
            response=mock.MagicMock(status_code=429),
            body=None,
        )
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            create_side_effect=rate_limit
        )
        with env_patch, ctor_patch:
            with self.assertRaises(OpenAIClientError) as ctx:
                openai_client.run_chat(chat_history=[], user_text="Hi")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertNotIsInstance(ctx.exception, OpenAIConfigError)

    def test_authentication_error_mapped_to_config_error_500(self):
        auth_err = openai.AuthenticationError(
            message="bad key",
            response=mock.MagicMock(status_code=401),
            body=None,
        )
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            create_side_effect=auth_err
        )
        with env_patch, ctor_patch:
            with self.assertRaises(OpenAIConfigError) as ctx:
                openai_client.run_chat(chat_history=[], user_text="Hi")
        self.assertEqual(ctx.exception.status_code, 500)

    def test_bad_request_error_mapped_to_400(self):
        bad_req = openai.BadRequestError(
            message="bad schema",
            response=mock.MagicMock(status_code=400),
            body=None,
        )
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            create_side_effect=bad_req
        )
        with env_patch, ctor_patch:
            with self.assertRaises(OpenAIClientError) as ctx:
                openai_client.run_chat(chat_history=[], user_text="Hi")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_timeout_error_mapped_to_504(self):
        timeout = openai.APITimeoutError(request=mock.MagicMock())
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            create_side_effect=timeout
        )
        with env_patch, ctor_patch:
            with self.assertRaises(OpenAIClientError) as ctx:
                openai_client.run_chat(chat_history=[], user_text="Hi")
        self.assertEqual(ctx.exception.status_code, 504)

    def test_connection_error_mapped_to_504(self):
        conn_err = openai.APIConnectionError(request=mock.MagicMock())
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            create_side_effect=conn_err
        )
        with env_patch, ctor_patch:
            with self.assertRaises(OpenAIClientError) as ctx:
                openai_client.run_chat(chat_history=[], user_text="Hi")
        self.assertEqual(ctx.exception.status_code, 504)

    def test_unknown_exception_wrapped_as_client_error_500(self):
        # Bare `except Exception` catch-all: a non-OpenAI exception from
        # client.responses.create() must become OpenAIClientError(500),
        # not propagate unwrapped.
        surprise = RuntimeError("surprise")
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            create_side_effect=surprise
        )
        with env_patch, ctor_patch:
            with self.assertRaises(OpenAIClientError) as ctx:
                openai_client.run_chat(chat_history=[], user_text="Hi")
        self.assertEqual(ctx.exception.status_code, 500)
        # Original cause is preserved via `from e` chaining.
        self.assertIs(ctx.exception.__cause__, surprise)

    def test_generic_api_status_error_passes_through_status_code(self):
        # A less-specific APIStatusError subclass that isn't individually
        # caught (e.g. InternalServerError → 500) should fall through to
        # the bare `except openai.APIStatusError` branch and preserve the
        # upstream status_code.
        internal = openai.InternalServerError(
            message="upstream boom",
            response=mock.MagicMock(status_code=500),
            body=None,
        )
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            create_side_effect=internal
        )
        with env_patch, ctor_patch:
            with self.assertRaises(OpenAIClientError) as ctx:
                openai_client.run_chat(chat_history=[], user_text="Hi")
        self.assertEqual(ctx.exception.status_code, 500)
        # Must not be reclassified as OpenAIConfigError (that's for auth).
        self.assertNotIsInstance(ctx.exception, OpenAIConfigError)


class TestRunStructured(unittest.TestCase):
    """run_structured calls client.responses.create with a JSON-schema
    text format and json.loads() the output."""

    def setUp(self):
        openai_client._reset_client_for_tests()

    def tearDown(self):
        openai_client._reset_client_for_tests()

    def _make_client_mock(self, output_text='{}'):
        fake_client = mock.MagicMock()
        fake_client.responses.create.return_value = _FakeResponse(
            output_text=output_text,
            usage=_FakeUsage(input_tokens=1, output_tokens=2, total_tokens=3),
            model="gpt-5.1",
        )
        env_patch = mock.patch.dict(os.environ, {"oaiKey": "sk-test"}, clear=True)
        ctor_patch = mock.patch(
            "datapipeline.openai_client.OpenAI",
            return_value=fake_client,
        )
        return fake_client, env_patch, ctor_patch

    def test_happy_path_parses_object_output(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            output_text='{"theme": "pace", "confidence": 0.9}'
        )
        with env_patch, ctor_patch:
            result = openai_client.run_structured(
                chat_history=[],
                user_text="analyze this",
                json_schema={"type": "object"},
            )
        self.assertEqual(
            result["parsed"],
            {"theme": "pace", "confidence": 0.9},
        )
        self.assertEqual(result["response"], '{"theme": "pace", "confidence": 0.9}')
        self.assertEqual(result["model"], "gpt-5.1")

    def test_happy_path_parses_array_output(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            output_text='[1, 2, 3]'
        )
        with env_patch, ctor_patch:
            result = openai_client.run_structured(
                chat_history=[],
                user_text="list ints",
                json_schema={"type": "array"},
            )
        self.assertEqual(result["parsed"], [1, 2, 3])

    def test_strict_mode_always_set(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
            "additionalProperties": False,
        }
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            output_text='{"x": "y"}'
        )
        with env_patch, ctor_patch:
            openai_client.run_structured(
                chat_history=[],
                user_text="do it",
                json_schema=schema,
                schema_name="my_schema",
            )
        _, kwargs = fake_client.responses.create.call_args
        self.assertEqual(
            kwargs["text"],
            {
                "format": {
                    "type": "json_schema",
                    "name": "my_schema",
                    "schema": schema,
                    "strict": True,
                }
            },
        )

    def test_default_schema_name(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            output_text='{}'
        )
        with env_patch, ctor_patch:
            openai_client.run_structured(
                chat_history=[],
                user_text="do it",
                json_schema={"type": "object"},
            )
        _, kwargs = fake_client.responses.create.call_args
        self.assertEqual(
            kwargs["text"]["format"]["name"],
            "structured_response",
        )

    def test_unparseable_output_raises_refusal_error(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            output_text="I cannot help with that request."
        )
        with env_patch, ctor_patch:
            with self.assertRaises(OpenAIRefusalError) as ctx:
                openai_client.run_structured(
                    chat_history=[],
                    user_text="harmful ask",
                    json_schema={"type": "object"},
                )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_default_model_used(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            output_text='{}'
        )
        with env_patch, ctor_patch:
            openai_client.run_structured(
                chat_history=[],
                user_text="do it",
                json_schema={"type": "object"},
            )
        _, kwargs = fake_client.responses.create.call_args
        self.assertEqual(kwargs["model"], DEFAULT_MODEL)

    def test_custom_model_passes_through(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            output_text='{}'
        )
        with env_patch, ctor_patch:
            openai_client.run_structured(
                chat_history=[],
                user_text="do it",
                json_schema={"type": "object"},
                model="gpt-4o",
            )
        _, kwargs = fake_client.responses.create.call_args
        self.assertEqual(kwargs["model"], "gpt-4o")

    def test_usage_translated(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            output_text='{}'
        )
        with env_patch, ctor_patch:
            result = openai_client.run_structured(
                chat_history=[],
                user_text="do it",
                json_schema={"type": "object"},
            )
        self.assertEqual(
            result["usage"],
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )

    def test_instructions_routed_from_system_messages(self):
        fake_client, env_patch, ctor_patch = self._make_client_mock(
            output_text='{}'
        )
        with env_patch, ctor_patch:
            openai_client.run_structured(
                chat_history=[{"role": "system", "content": "Output strict JSON."}],
                user_text="do it",
                json_schema={"type": "object"},
            )
        _, kwargs = fake_client.responses.create.call_args
        self.assertEqual(kwargs["instructions"], "Output strict JSON.")
