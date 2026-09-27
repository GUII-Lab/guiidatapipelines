"""Revision routing uses only the new message and validated survey questions."""

import json
from pathlib import Path
from unittest import TestCase

from leai.services.revision_assessment import classify_revision
from leai.services.structured_assessment import AssessmentError


PROTOCOL = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "ulia_conversational.json").read_text())


class RevisionAssessmentTests(TestCase):
    def test_maps_an_explicit_correction_without_prior_student_text(self):
        observed = {}

        def provider(messages, text, schema, *, schema_name):
            observed["messages"] = messages
            observed["schema"] = schema
            observed["text"] = text
            return {"parsed": {"item_id": "P1", "operation": "replace", "answer_text": "I remove private details.",
                               "clarification": "", "rating": None}}

        result = classify_revision(PROTOCOL, "Change P1: I remove private details.",
                                   current_item_id="P2", provider=provider)
        self.assertEqual(result["item_id"], "P1")
        self.assertEqual(result["operation"], "replace")
        self.assertNotIn("previous student answer", json.dumps(observed["messages"]).lower())
        self.assertEqual(observed["text"], "Change P1: I remove private details.")
        self.assertIn("P1", observed["schema"]["properties"]["item_id"]["enum"])

    def test_rejects_unknown_target_or_rating_on_text_question(self):
        for parsed in (
            {"item_id": "P100", "operation": "replace", "answer_text": "new", "clarification": "", "rating": None},
            {"item_id": "P1", "operation": "replace", "answer_text": "new", "clarification": "", "rating": 3},
        ):
            with self.assertRaises(AssessmentError):
                classify_revision(PROTOCOL, "change answer", provider=lambda *_args, **_kwargs: {"parsed": parsed})

    def test_can_classify_a_normal_current_answer_without_rewriting_it(self):
        result = classify_revision(PROTOCOL, "Actually, I focus on context.", current_item_id="P1",
                                   provider=lambda *_args, **_kwargs: {"parsed": {
                                       "item_id": "", "operation": "answer", "answer_text": "",
                                       "clarification": "", "rating": None,
                                   }})
        self.assertEqual(result["operation"], "answer")
