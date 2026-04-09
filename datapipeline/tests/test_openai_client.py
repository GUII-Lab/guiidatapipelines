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
