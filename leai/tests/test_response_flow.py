"""Student turn-order tests against Ulia's two complete protocols."""

import json
from pathlib import Path

from django.test import SimpleTestCase

from leai.services.response_flow import FlowError, apply_turn, begin_flow, current_prompt


ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def load(name):
    return json.loads((ROOT / name).read_text())


class ResponseFlowTests(SimpleTestCase):
    def test_likert_rating_needs_explicit_choice_then_reflection_before_p2(self):
        protocol = load("ulia_likert.json")
        state = begin_flow(protocol)
        self.assertEqual(current_prompt(protocol, state)["item_id"], "P1")
        self.assertEqual(current_prompt(protocol, state)["phase"], "rating")
        with self.assertRaises(FlowError):
            apply_turn(protocol, state, {"item_id": "P1", "kind": "text", "text": "I agree"})
        state = apply_turn(protocol, state, {"item_id": "P1", "kind": "rating", "value": 4})
        self.assertEqual(current_prompt(protocol, state)["phase"], "reflection")
        self.assertEqual(state["results"]["P1"]["rating"], 4)
        state = apply_turn(protocol, state, {"item_id": "P1", "kind": "text", "text": "I avoid personal information."},
                           assessment={"sufficient": True, "evidence_for": ["P1"]})
        self.assertEqual(current_prompt(protocol, state)["item_id"], "P2")
        self.assertEqual(current_prompt(protocol, state)["phase"], "rating")
        self.assertNotIn("rating", state["results"].get("P2", {}))

    def test_cross_item_evidence_does_not_skip_next_main_question(self):
        protocol = load("ulia_conversational.json")
        state = begin_flow(protocol)
        state = apply_turn(protocol, state, {
            "item_id": "P1", "kind": "text",
            "text": "I remove private data, and I also check what context the AI needs.",
        }, assessment={"sufficient": True, "evidence_for": ["P1", "P2"],
                       "covered_targets": ["decision_process"]})
        self.assertEqual(current_prompt(protocol, state)["item_id"], "P2")
        self.assertIn("P2", state["evidence_seen"])
        self.assertEqual(current_prompt(protocol, state)["text"], protocol["sections"][0]["items"][1]["prompt"])
        self.assertIn("confirm or clarify", current_prompt(protocol, state)["context_note"])

    def test_partial_answer_probes_until_sufficient_then_advances(self):
        protocol = load("ulia_conversational.json")
        state = begin_flow(protocol)
        state = apply_turn(protocol, state, {"item_id": "P1", "kind": "text", "text": "Yes."},
                           assessment={"sufficient": False, "followup": "How do you decide?", "evidence_for": ["P1"]})
        self.assertEqual(current_prompt(protocol, state)["phase"], "probe")
        self.assertEqual(current_prompt(protocol, state)["text"], "How do you decide?")
        state = apply_turn(protocol, state, {"item_id": "P1", "kind": "text", "text": "I give assignment context."},
                           assessment={"sufficient": True, "evidence_for": ["P1"],
                                       "covered_targets": ["decision_process"]})
        self.assertEqual(current_prompt(protocol, state)["item_id"], "P2")

    def test_decline_is_not_the_neutral_rating(self):
        protocol = load("ulia_likert.json")
        state = apply_turn(protocol, begin_flow(protocol), {"item_id": "P1", "kind": "skip"})
        self.assertEqual(state["results"]["P1"]["status"], "declined")
        self.assertIsNone(state["results"]["P1"]["rating"])
        self.assertEqual(current_prompt(protocol, state)["item_id"], "P2")

    def test_stale_item_cannot_be_answered_again(self):
        protocol = load("ulia_conversational.json")
        state = apply_turn(protocol, begin_flow(protocol), {"item_id": "P1", "kind": "skip"})
        with self.assertRaises(FlowError):
            apply_turn(protocol, state, {"item_id": "P1", "kind": "skip"})

    def test_probe_limit_stops_repetitive_followups_without_marking_answer_sufficient(self):
        protocol = load("ulia_conversational.json")
        state = begin_flow(protocol)
        for index in range(2):
            state = apply_turn(protocol, state, {"item_id": "P1", "kind": "text", "text": "I am still considering."},
                               assessment={"sufficient": False, "followup": f"Could you clarify {index}?", "evidence_for": []})
            self.assertEqual(current_prompt(protocol, state)["phase"], "probe")
        state = apply_turn(protocol, state, {"item_id": "P1", "kind": "text", "text": "I am not sure."},
                           assessment={"sufficient": False, "followup": "Another question?", "evidence_for": []})
        self.assertEqual(state["results"]["P1"], {"rating": None, "status": "partial", "probes": 2})
        self.assertEqual(current_prompt(protocol, state)["item_id"], "P2")

    def test_duplicate_followup_does_not_repeat_the_same_probe(self):
        protocol = load("ulia_conversational.json")
        state = apply_turn(protocol, begin_flow(protocol), {"item_id": "P1", "kind": "text", "text": "Yes."},
                           assessment={"sufficient": False, "followup": "How do you decide?", "evidence_for": []})
        state = apply_turn(protocol, state, {"item_id": "P1", "kind": "text", "text": "Not sure."},
                           assessment={"sufficient": False, "followup": "How do you decide?", "evidence_for": []})
        self.assertEqual(state["results"]["P1"]["status"], "partial")
        self.assertEqual(current_prompt(protocol, state)["item_id"], "P2")

    def test_coverage_targets_accumulate_across_turns_without_retransmitting_prior_text(self):
        protocol = load("ulia_conversational.json")
        protocol["sections"][0]["items"][0]["coverage_targets"] = [
            {"id": "what_to_give", "description": "What information to give"},
            {"id": "what_to_omit", "description": "What information not to give"},
        ]
        state = apply_turn(protocol, begin_flow(protocol), {"item_id": "P1", "kind": "text", "text": "Task context."},
                           assessment={"sufficient": False, "followup": "What would you omit?", "evidence_for": ["P1"],
                                       "covered_targets": ["what_to_give"]})
        self.assertEqual(current_prompt(protocol, state)["phase"], "probe")
        state = apply_turn(protocol, state, {"item_id": "P1", "kind": "text", "text": "Private details."},
                           assessment={"sufficient": False, "followup": "Could you say more?", "evidence_for": ["P1"],
                                       "covered_targets": ["what_to_omit"]})
        self.assertEqual(state["coverage_seen"]["P1"], ["what_to_give", "what_to_omit"])
        self.assertEqual(state["results"]["P1"]["status"], "answered")
        self.assertEqual(current_prompt(protocol, state)["item_id"], "P2")
