import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from leai.services.authoring_orchestrator import run_authoring_orchestrator
from leai.services.authoring_wizard import initial_body


class _ToolCall:
    type = "function_call"

    def __init__(self, name, arguments, call_id):
        self.name = name
        self.arguments = json.dumps(arguments)
        self.call_id = call_id

    def model_dump(self, exclude_none=True):
        return {
            "type": "function_call", "name": self.name,
            "arguments": self.arguments, "call_id": self.call_id,
        }


class _Client:
    def __init__(self, responses):
        self.responses = self
        self.pending = responses
        self.calls = []

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.pending.pop(0)


class AuthoringOrchestratorTests(SimpleTestCase):
    def setUp(self):
        self.body = initial_body(title="Practice", audience="individual", collection_style="guided")

    def test_tool_calls_read_then_validate_proposal_before_returning(self):
        candidate = json.loads(json.dumps(self.body))
        candidate["sections"][0]["items"][0]["prompt"] = "What helped your learning this week?"
        client = _Client([
            SimpleNamespace(status="completed", output=[_ToolCall("read_draft", {}, "read")], output_text=""),
            SimpleNamespace(status="completed", output=[_ToolCall("preview_revision", {"body": candidate, "summary": "Updated one question"}, "preview")], output_text=""),
            SimpleNamespace(status="completed", output=[], output_text="I updated the first question."),
        ])
        result, reply = run_authoring_orchestrator(self.body, "Change the first question.", [], client=client)
        self.assertEqual(result, candidate)
        self.assertEqual(reply, "I updated the first question.")
        self.assertEqual(len(client.calls), 3)
        self.assertFalse(any(call["store"] for call in client.calls))
        self.assertIn("function_call_output", str(client.calls[1]["input"]))

    def test_final_text_without_validated_tool_proposal_fails_closed(self):
        client = _Client([SimpleNamespace(status="completed", output=[], output_text="Done.")])
        with self.assertRaisesMessage(ValueError, "missing_validated_proposal"):
            run_authoring_orchestrator(self.body, "Add a question.", [], client=client)

    def test_invalid_candidate_is_not_returned(self):
        bad = {"title": "Oops"}
        client = _Client([
            SimpleNamespace(status="completed", output=[_ToolCall("read_draft", {}, "read")], output_text=""),
            SimpleNamespace(status="completed", output=[_ToolCall("preview_revision", {"body": bad, "summary": "Bad"}, "preview")], output_text=""),
            SimpleNamespace(status="completed", output=[], output_text="Done."),
        ])
        with self.assertRaisesMessage(ValueError, "missing_validated_proposal"):
            run_authoring_orchestrator(self.body, "Add a question.", [], client=client)
