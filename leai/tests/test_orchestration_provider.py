"""Provider-boundary tests use a transport double, never a mocked orchestrator."""
import importlib
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from leai.tests.test_orchestration import proposal, update
from leai.services.response_flow import begin_flow
from pathlib import Path


class ProviderTests(TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("datapipeline.orchestration_client"), "instrumented transport not implemented")
        self.module = importlib.import_module("datapipeline.orchestration_client")
        self.protocol = json.loads((Path(__file__).parents[1] / "fixtures/ulia_conversational.json").read_text())
        self.state = begin_flow(self.protocol)
        self.messages = [{"sequence": 3, "role": "student", "content": "I use the task goal."}]

    def response(self, p=None, calls=()):
        return SimpleNamespace(status="completed", id="resp_test", _request_id="req_test", model="test-model",
                               service_tier="default", incomplete_details=None,
                               output_text=json.dumps(p or proposal()), output=list(calls),
                               usage=SimpleNamespace(input_tokens=100, output_tokens=30, total_tokens=130,
                                   input_tokens_details=SimpleNamespace(cached_tokens=40),
                                   output_tokens_details=SimpleNamespace(reasoning_tokens=10)))

    def run_provider(self, responses, tools=False):
        client = Mock(); client.with_options.return_value = client
        client.responses.create.side_effect = responses
        result = self.module.run_orchestration(self.protocol, self.state, self.messages, tools_enabled=tools, client=client)
        self.assertEqual(client.with_options.call_args.kwargs["max_retries"], 0)
        self.assertIs(client.responses.create.call_args.kwargs["store"], False)
        return result

    def test_records_actual_usage_not_double_counting_reasoning_and_cache(self):
        result = self.run_provider([self.response()])
        call = result["metrics"]["calls"][0]
        self.assertEqual(call["input_tokens"], 100)
        self.assertEqual(call["cached_input_tokens"], 40)
        self.assertEqual(call["output_tokens"], 30)
        self.assertEqual(call["reasoning_tokens"], 10)
        self.assertEqual(call["total_tokens"], 130)
        self.assertGreaterEqual(call["duration_ms"], 0)
        self.assertEqual(call["request_id"], "req_test")
        self.assertIsNone(call["ttft_ms"])

    def test_missing_usage_stays_unknown(self):
        r = self.response(); r.usage = None
        call = self.run_provider([r])["metrics"]["calls"][0]
        self.assertIsNone(call["input_tokens"])
        self.assertIsNone(call["total_tokens"])

    def test_invalid_evidence_repaired_once_and_both_calls_counted(self):
        bad = update(); bad["evidence"][0]["quote"] = "made up"
        result = self.run_provider([self.response(proposal([bad])), self.response()])
        self.assertEqual(len(result["metrics"]["calls"]), 2)
        self.assertEqual(result["metrics"]["repair_count"], 1)
        self.assertEqual(result["state"]["answer_map"], {})

    def test_read_tool_cannot_reach_another_session(self):
        call = SimpleNamespace(type="function_call", name="get_evidence", call_id="call_1", arguments='{"sequences":[999]}')
        call.model_dump = lambda **kw: {"type": "function_call", "name": call.name, "call_id": call.call_id, "arguments": call.arguments}
        result = self.run_provider([self.response(calls=[call]), self.response()], tools=True)
        self.assertEqual(result["metrics"]["tools"][0]["outcome"], "rejected")
        self.assertEqual(result["state"]["answer_map"], {})

    def test_exhausted_repair_returns_no_mutation_and_keeps_usage(self):
        bad = update(); bad["evidence"][0]["quote"] = "made up"
        with self.assertRaises(self.module.OrchestrationUnavailable) as raised:
            self.run_provider([self.response(proposal([bad])), self.response(proposal([bad]))])
        self.assertEqual(len(raised.exception.metrics["calls"]), 2)
        self.assertEqual(self.state["results"], {})
