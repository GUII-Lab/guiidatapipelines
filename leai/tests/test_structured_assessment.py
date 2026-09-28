"""The model may judge coverage, but cannot change exact stems or scale answers."""

import json
from copy import deepcopy
from pathlib import Path

from django.test import SimpleTestCase

from leai.services.response_flow import begin_flow
from leai.services.structured_assessment import AssessmentError, assess_text


PROTOCOL = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "ulia_conversational.json").read_text())


class AssessmentTests(SimpleTestCase):
    def test_clarification_request_gets_a_plain_language_example_without_becoming_an_answer(self):
        reply = "By limitations, I mean things that can affect how you use AI, such as not knowing a topic well or having limited time. Which, if any, come to mind for you?"

        def provider(history, student_text, json_schema, **kwargs):
            return {"parsed": {
                "intent": "clarification",
                "clarification_response": reply,
                "sufficient": False,
                "followup": "",
                "evidence_for": [],
                "covered_targets": [],
            }}

        result = assess_text(PROTOCOL, begin_flow(PROTOCOL), "What do you mean? Can you give an example?",
                             provider=provider)

        self.assertEqual(result["intent"], "clarification")
        self.assertEqual(result["clarification_response"], reply)
        self.assertFalse(result["sufficient"])

    def test_clarification_assessment_includes_the_exact_pending_followup_the_student_means_by_that(self):
        state = begin_flow(PROTOCOL)
        state["phase"] = "probe"
        state["pending_prompt"] = "How do you decide what information to share?"
        calls = []

        def provider(history, student_text, json_schema, **kwargs):
            calls.append(history[0]["content"])
            return {"parsed": {
                "intent": "clarification",
                "clarification_response": "I mean, what helps you decide which details the AI needs?",
                "sufficient": False, "followup": "", "evidence_for": [], "covered_targets": [],
            }}

        assess_text(PROTOCOL, state, "What do you mean by that?", provider=provider)

        self.assertIn("How do you decide what information to share?", calls[0])

    def test_required_coverage_target_prevents_bare_yes_from_skipping_item(self):
        protocol = deepcopy(PROTOCOL)
        protocol["sections"][0]["items"][0]["coverage_targets"] = [
            {"id": "decision_process", "description": "If yes, explain how the student decides what information to give."},
        ]

        def provider(history, student_text, json_schema, **kwargs):
            return {"parsed": {"intent": "answer", "clarification_response": "",
                               "sufficient": True, "followup": "", "evidence_for": ["P1"],
                               "covered_targets": []}}

        result = assess_text(protocol, begin_flow(protocol), "Yes.", provider=provider)
        self.assertFalse(result["sufficient"])
        self.assertIn("how do you decide", result["followup"].lower())

    def test_model_receives_current_goal_and_can_attribute_future_item_evidence(self):
        calls = []

        def provider(history, student_text, json_schema, **kwargs):
            calls.append((history, student_text, json_schema))
            return {"parsed": {"intent": "answer", "clarification_response": "",
                               "sufficient": True, "followup": "", "evidence_for": ["P1", "P2"],
                               "covered_targets": []}}

        result = assess_text(PROTOCOL, begin_flow(PROTOCOL),
                             "I remove private details and check what context the AI needs.", provider=provider)
        self.assertEqual(result["evidence_for"], ["P1", "P2"])
        self.assertIn("P1", calls[0][0][0]["content"])
        self.assertIn("whether and how", calls[0][0][0]["content"])
        self.assertNotIn("I remove private details", calls[0][0][0]["content"])

    def test_open_first_answer_gets_a_topic_followup_before_closing(self):
        protocol = deepcopy(PROTOCOL)
        protocol["sections"][0]["items"][0]["response"] = {"kind": "text"}
        protocol["sections"][0]["items"][0]["max_additional_probes"] = 3
        def provider(history, student_text, json_schema, **kwargs):
            self.assertIn("Open conversation", history[0]["content"])
            return {"parsed": {"intent": "answer", "clarification_response": "",
                               "sufficient": True, "followup": "Which part of that workload felt hardest?",
                               "evidence_for": ["P1"], "covered_targets": []}}
        result = assess_text(protocol, begin_flow(protocol), "The workload felt heavy.",
                             provider=provider, force_followup=True)
        self.assertFalse(result["sufficient"])
        self.assertEqual(result["followup"], "Which part of that workload felt hardest?")

    def test_model_cannot_attribute_nonexistent_item_or_probe_with_next_stem(self):
        def unknown_item(*args, **kwargs):
            return {"parsed": {"intent": "answer", "clarification_response": "",
                               "sufficient": False, "followup": "How?", "evidence_for": ["X99"],
                               "covered_targets": []}}

        with self.assertRaises(AssessmentError):
            assess_text(PROTOCOL, begin_flow(PROTOCOL), "Yes", provider=unknown_item)

        next_stem = PROTOCOL["sections"][0]["items"][1]["prompt"]

        def next_question(*args, **kwargs):
            return {"parsed": {"intent": "answer", "clarification_response": "",
                               "sufficient": False, "followup": next_stem, "evidence_for": ["P1"],
                               "covered_targets": []}}

        with self.assertRaises(AssessmentError):
            assess_text(PROTOCOL, begin_flow(PROTOCOL), "Yes", provider=next_question)
