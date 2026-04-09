"""View-level tests for /api/openai-chat/ and /api/openai-structured/.

These use Django's test client but mock out datapipeline.openai_client so
no real SDK calls happen."""
import json
from unittest import mock

from django.test import Client, TestCase

from datapipeline import openai_client


class TestOpenAIChatView(TestCase):
    url = "/datapipeline/api/openai-chat/"

    def setUp(self):
        self.client = Client()

    def _post(self, body: dict):
        return self.client.post(
            self.url,
            data=json.dumps(body),
            content_type="application/json",
        )

    @mock.patch("datapipeline.views.openai_client.run_chat")
    def test_happy_path_returns_success_shape(self, mock_run_chat):
        mock_run_chat.return_value = {
            "response": "hello back",
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            "model": "gpt-5.1",
        }
        response = self._post({
            "chat_history": [],
            "user_text": "hi",
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["response"], "hello back")
        self.assertEqual(
            body["usage"],
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )
        # Verify chat_history + user_text passed through to the client module
        mock_run_chat.assert_called_once_with(
            chat_history=[],
            user_text="hi",
            model=None,
        )

    @mock.patch("datapipeline.views.openai_client.run_chat")
    def test_model_override_passed_through(self, mock_run_chat):
        mock_run_chat.return_value = {
            "response": "ok", "usage": {}, "model": "gpt-4o",
        }
        self._post({
            "chat_history": [],
            "user_text": "hi",
            "model": "gpt-4o",
        })
        mock_run_chat.assert_called_once()
        _, kwargs = mock_run_chat.call_args
        self.assertEqual(kwargs["model"], "gpt-4o")

    def test_missing_user_text_returns_400(self):
        response = self._post({"chat_history": []})
        self.assertEqual(response.status_code, 400)
        self.assertIn("user_text", response.json()["error"])

    def test_non_post_returns_405(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_invalid_json_body_returns_400(self):
        response = self.client.post(
            self.url,
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    @mock.patch("datapipeline.views.openai_client.run_chat")
    def test_client_error_mapped_to_status_code(self, mock_run_chat):
        mock_run_chat.side_effect = openai_client.OpenAIClientError(
            "rate limited", status_code=429,
        )
        response = self._post({"chat_history": [], "user_text": "hi"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("rate limited", response.json()["error"])

    @mock.patch("datapipeline.views.openai_client.run_chat")
    def test_config_error_mapped_to_500(self, mock_run_chat):
        mock_run_chat.side_effect = openai_client.OpenAIConfigError(
            "missing key",
        )
        response = self._post({"chat_history": [], "user_text": "hi"})
        self.assertEqual(response.status_code, 500)
