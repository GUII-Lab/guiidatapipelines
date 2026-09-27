"""Persisted progress for anonymous structured reflection sessions."""

import json
from pathlib import Path

from django.test import TestCase

from leai.services.response_flow import begin_flow
from leai.tests.test_response_models import ResponseFixturesMixin


class StructuredSessionTests(ResponseFixturesMixin, TestCase):
    def test_item_cursor_and_rating_survive_a_database_reload(self):
        protocol = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "ulia_likert.json").read_text())
        session = self.make_student_session()
        session.flow_state = begin_flow(protocol)
        session.save(update_fields=["flow_state"])
        session.refresh_from_db()
        self.assertEqual(session.flow_state["phase"], "rating")
        self.assertEqual(session.flow_state["item_index"], 0)
