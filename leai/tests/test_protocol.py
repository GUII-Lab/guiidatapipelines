"""Contract tests for versioned conversational and Likert protocols."""

from copy import deepcopy
import json
from pathlib import Path

from django.test import SimpleTestCase

from leai.services.protocol import ProtocolError, validate_protocol


LIKERT_PROTOCOL = {
    "version": 1,
    "title": "AI reflection",
    "intro": "Please rate each statement and reflect on your answer.",
    "scales": {
        "agreement_5": [
            {"value": 1, "label": "Strongly disagree"},
            {"value": 2, "label": "Disagree"},
            {"value": 3, "label": "Neither agree nor disagree"},
            {"value": 4, "label": "Agree"},
            {"value": 5, "label": "Strongly agree"},
        ]
    },
    "sections": [{
        "id": "planning",
        "title": "Planning",
        "items": [{
            "id": "P1",
            "prompt": "I think about how to give the most appropriate information to the AI.",
            "wording": "exact",
            "response": {"kind": "likert", "scale_id": "agreement_5"},
            "reflection_goal": "Explain the selected rating with a reason or example.",
            "coverage_targets": [],
            "example_probes": ["How do you decide what to give the AI?"],
            "max_additional_probes": 2,
        }],
    }],
}


class ProtocolContractTests(SimpleTestCase):
    def test_accepts_exact_likert_statement_with_contextual_reflection(self):
        validated = validate_protocol(LIKERT_PROTOCOL)
        self.assertEqual(validated["sections"][0]["items"][0]["id"], "P1")

    def test_accepts_conversational_item_without_a_scale(self):
        protocol = deepcopy(LIKERT_PROTOCOL)
        protocol["scales"] = {}
        item = protocol["sections"][0]["items"][0]
        item["prompt"] = "Do you think about what information to give the AI?"
        item["response"] = {"kind": "text"}
        self.assertEqual(validate_protocol(protocol)["sections"][0]["items"][0]["response"]["kind"], "text")

    def test_rejects_missing_scale_and_duplicate_item_ids(self):
        protocol = deepcopy(LIKERT_PROTOCOL)
        protocol["sections"][0]["items"][0]["response"]["scale_id"] = "missing"
        with self.assertRaises(ProtocolError):
            validate_protocol(protocol)

        protocol = deepcopy(LIKERT_PROTOCOL)
        protocol["sections"][0]["items"].append(deepcopy(protocol["sections"][0]["items"][0]))
        with self.assertRaises(ProtocolError):
            validate_protocol(protocol)

    def test_ulia_option_1_and_2_cover_all_eleven_source_items(self):
        expected_ids = ["P1", "P2", "P3", "P4", "M1", "M2", "M3", "M4", "E1", "E2", "E3"]
        root = Path(__file__).resolve().parents[1] / "fixtures"
        protocols = []
        for name in ("ulia_conversational.json", "ulia_likert.json"):
            protocol = validate_protocol(json.loads((root / name).read_text()))
            items = [item for section in protocol["sections"] for item in section["items"]]
            self.assertEqual([item["id"] for item in items], expected_ids)
            protocols.append(items)
        self.assertTrue(all(item["response"]["kind"] == "text" for item in protocols[0]))
        self.assertTrue(all(item["response"]["kind"] == "likert" for item in protocols[1]))
        self.assertEqual(protocols[0][0]["prompt"],
                         "Do you think about what is the most appropriate information to give to the AI?")
        self.assertEqual(protocols[1][0]["prompt"],
                         "I think about how to give the most appropriate information to the AI.")
