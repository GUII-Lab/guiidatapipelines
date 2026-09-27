"""Anonymous, occurrence-scoped student protocol exercised through HTTP."""

import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from leai.models import InstitutionMembership, ResponseMessage, ResponseSession
from leai.services.structured_assessment import AssessmentError
from leai.tests.test_response_models import ResponseFixturesMixin
from leai.tests.session_client import SessionClient


ROOT = Path(__file__).resolve().parents[1] / "fixtures"
API = "/datapipeline/api/v1/surveys/"
CONSENT = json.dumps({"terms_consent": True, "research_consent": False})


class StudentResponseApiTests(ResponseFixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.account.must_change_password = False
        self.account.save(update_fields=["must_change_password"])
        self.account.user.set_password("Research-Debug-Only-2026!")
        self.account.user.save(update_fields=["password"])
        InstitutionMembership.objects.create(
            account=self.account,
            institution=self.institution,
            role="researcher",
        )
        self.researcher = SessionClient()
        login = self.researcher.post(
            "/datapipeline/api/v1/instructor_sessions/",
            data=json.dumps({
                "email": self.account.email,
                "password": "Research-Debug-Only-2026!",
            }),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 201)
        self.occurrence = self.make_occurrence(
            compiled_protocol=json.loads((ROOT / "ulia_likert.json").read_text()),
        )
        self.base = f"{API}{self.occurrence.public_id}/"

    def _start(self):
        return self.client.post(self.base + "sessions/", data=CONSENT, content_type="application/json")

    def test_session_start_requires_terms_consent_and_saves_optional_research_choice(self):
        url = self.base + "sessions/"
        for body in ({}, {"terms_consent": False, "research_consent": True},
                     {"terms_consent": True, "research_consent": "yes"}):
            response = self.client.post(url, data=json.dumps(body), content_type="application/json")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(ResponseSession.objects.count(), 0)
        for research in (False, True):
            response = self.client.post(url, data=json.dumps({
                "terms_consent": True, "research_consent": research,
            }), content_type="application/json")
            self.assertEqual(response.status_code, 201)
            saved = ResponseSession.objects.get(public_id=response.json()["session_id"])
            self.assertEqual(saved.research_consent, research)

    def test_public_survey_exposes_only_its_student_output_settings(self):
        self.occurrence.completion_certificate_enabled = True
        self.occurrence.completed_response_download_enabled = True
        self.occurrence.save(update_fields=["completion_certificate_enabled", "completed_response_download_enabled"])
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["completion_certificate_enabled"])
        self.assertTrue(response.json()["completed_response_download_enabled"])

    def test_debug_requires_researcher_and_enabled_course_setting_and_is_read_only(self):
        started = self._start().json()
        url = self.base + f"sessions/{started['session_id']}/debug/"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {started['token']}").status_code, 401)
        self.assertEqual(self.researcher.get(url).status_code, 404)
        self.course.student_debug_enabled = True
        self.course.save(update_fields=["student_debug_enabled"])
        response = self.researcher.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json()["schema_state"]["phase"], "rating")
        self.assertEqual(response.json()["responses"], [])
        self.assertNotIn("token", response.json())
        self.assertNotIn("capability_digest", response.content.decode())
        self.assertEqual(ResponseSession.objects.get(public_id=started["session_id"]).turn_version, 1)

    def test_debug_is_unavailable_in_production_even_with_session_capability(self):
        started = self._start().json()
        url = self.base + f"sessions/{started['session_id']}/debug/"
        self.course.student_debug_enabled = True
        self.course.save(update_fields=["student_debug_enabled"])
        with override_settings(LEAI_ENVIRONMENT="production"), patch(
            "leai.api.environment.verified_environment_identity", return_value={"environment": "production"}
        ):
            response = self.researcher.get(url)
        self.assertEqual(response.status_code, 404)

    def test_debug_shows_persisted_rating_coverage_and_cross_item_evidence(self):
        started = self._start().json()
        session_url = self.base + f"sessions/{started['session_id']}/"
        student_auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        rated = self.client.post(session_url + "turns/", data=json.dumps({
            "expected_version": 1, "item_id": "P1", "kind": "rating", "value": 4,
        }), content_type="application/json", **student_auth)
        self.assertEqual(rated.status_code, 200)
        with patch("leai.api.student_responses.assess_text", return_value={
            "sufficient": False, "followup": "What did you leave out?",
            "evidence_for": ["P1", "P2"], "covered_targets": [],
        }):
            answered = self.client.post(session_url + "turns/", data=json.dumps({
                "expected_version": 2, "item_id": "P1", "kind": "text",
                "text": "I choose useful context and identify what the next task needs.",
            }), content_type="application/json", **student_auth)
        self.assertEqual(answered.status_code, 200)
        self.course.student_debug_enabled = True
        self.course.save(update_fields=["student_debug_enabled"])
        debug = self.researcher.get(session_url + "debug/").json()
        self.assertEqual(debug["turn_version"], 3)
        self.assertEqual(debug["schema_state"]["phase"], "probe")
        self.assertEqual(debug["schema_state"]["results"]["P1"],
                         {"rating": 4, "status": "active", "probes": 1})
        self.assertEqual(debug["schema_state"]["evidence_seen"], {"P1": True, "P2": True})
        self.assertEqual(debug["responses"][1]["item_id"], "P1")
        self.assertEqual(debug["responses"][1]["evidence_for"], ["P1", "P2"])
        self.assertEqual(debug["responses"][1]["content"],
                         "I choose useful context and identify what the next task needs.")
        self.assertEqual(debug["responses"][1]["next_phase"], "probe")
        self.assertEqual(debug["responses"][1]["next_item_id"], "P1")
        self.assertNotIn("sufficient", debug["responses"][1])

    def test_debug_identifies_the_next_question_after_a_skip(self):
        protocol = json.loads((ROOT / "ulia_conversational.json").read_text())
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        base = f"{API}{occurrence.public_id}/"
        started = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        student_auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        session_url = base + f"sessions/{started['session_id']}/"
        response = self.client.post(session_url + "turns/", data=json.dumps({
            "expected_version": 1, "item_id": "P1", "kind": "skip",
        }), content_type="application/json", **student_auth)
        self.assertEqual(response.status_code, 200)
        self.course.student_debug_enabled = True
        self.course.save(update_fields=["student_debug_enabled"])
        debug = self.researcher.get(session_url + "debug/").json()
        self.assertEqual(debug["responses"][0]["next_item_id"], "P2")
        self.assertEqual(debug["responses"][0]["next_phase"], "answer")

    def test_new_session_returns_exact_p1_and_persists_only_assistant_opening(self):
        response = self._start()
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["progress_label"], "Area 1 of 3 — Planning · Question 1 of 4")
        self.assertTrue(payload["messages"][0]["created_at"].endswith("+00:00"))
        self.assertEqual(payload["prompt"]["item_id"], "P1")
        self.assertEqual(payload["prompt"]["phase"], "rating")
        self.assertEqual(payload["prompt"]["text"],
                         "I think about how to give the most appropriate information to the AI.")
        session = ResponseSession.objects.get(public_id=payload["session_id"])
        self.assertNotEqual(session.capability_digest, payload["token"])
        self.assertEqual(ResponseMessage.objects.filter(response_session=session, role="student").count(), 0)
        self.assertEqual(session.flow_state["item_index"], 0)

    def test_finalization_requires_the_full_question_flow_and_own_capability(self):
        started = self._start().json()
        url = self.base + f"sessions/{started['session_id']}/finalize/"
        payload = json.dumps({"expected_version": 1})
        self.assertEqual(self.client.post(url, data=payload, content_type="application/json").status_code, 404)
        other = self._start().json()
        self.assertEqual(self.client.post(url, data=payload, content_type="application/json",
                                          HTTP_AUTHORIZATION=f"Bearer {other['token']}").status_code, 404)
        response = self.client.post(url, data=payload, content_type="application/json",
                                    HTTP_AUTHORIZATION=f"Bearer {started['token']}")
        self.assertEqual(response.status_code, 422)
        session = ResponseSession.objects.get(public_id=started["session_id"])
        self.assertEqual(session.status, "active")
        self.assertIsNone(session.completion_snapshot)

    def test_rating_then_reflection_survives_reload_and_advances_to_p2(self):
        started = self._start().json()
        token = started["token"]
        url = self.base + f"sessions/{started['session_id']}/"
        auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        rating = self.client.post(url + "turns/", data=json.dumps({
            "expected_version": 1, "item_id": "P1", "kind": "rating", "value": 4,
        }), content_type="application/json", **auth)
        self.assertEqual(rating.status_code, 200)
        self.assertEqual(rating.json()["prompt"]["phase"], "reflection")
        self.assertEqual(self.client.get(url, **auth).json()["results"]["P1"]["rating"], 4)
        with patch("leai.api.student_responses.assess_text", return_value={
            "sufficient": True, "followup": "", "evidence_for": ["P1", "P2"], "covered_targets": [],
        }):
            reflection = self.client.post(url + "turns/", data=json.dumps({
                "expected_version": 2, "item_id": "P1", "kind": "text",
                "text": "I avoid private details and check what task context AI needs.",
            }), content_type="application/json", **auth)
        self.assertEqual(reflection.status_code, 200)
        self.assertEqual(reflection.json()["prompt"]["item_id"], "P2")
        self.assertEqual(reflection.json()["prompt"]["phase"], "rating")
        self.assertNotIn("P2", reflection.json()["results"])
        self.assertEqual(ResponseMessage.objects.filter(role="student", response_session__public_id=started["session_id"]).count(), 2)
        self.assertEqual(self.client.post(url + "turns/", data=json.dumps({
            "expected_version": 2, "item_id": "P1", "kind": "text", "text": "Duplicate",
        }), content_type="application/json", **auth).status_code, 409)

    def test_capability_is_required_and_survey_closed_to_new_sessions(self):
        started = self._start().json()
        url = self.base + f"sessions/{started['session_id']}/"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.occurrence.manually_closed_at = self.occurrence.created_at
        self.occurrence.save(update_fields=["manually_closed_at"])
        self.assertEqual(self._start().status_code, 403)

    def test_all_eleven_likert_items_can_complete_with_distinct_ratings_and_reflections(self):
        started = self._start().json()
        token = started["token"]
        url = self.base + f"sessions/{started['session_id']}/turns/"
        auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        current = started
        item_ids = [item["id"] for section in self.occurrence.revision.compiled_protocol["sections"]
                    for item in section["items"]]
        with patch("leai.api.student_responses.assess_text", side_effect=lambda protocol, state, text: {
            "sufficient": True, "followup": "", "evidence_for": [item_ids[state["item_index"]]],
            "covered_targets": [],
        }):
            for item_id in item_ids:
                self.assertEqual(current["prompt"]["item_id"], item_id)
                self.assertEqual(current["prompt"]["phase"], "rating")
                rated = self.client.post(url, data=json.dumps({
                    "expected_version": current["turn_version"], "item_id": item_id,
                    "kind": "rating", "value": 4,
                }), content_type="application/json", **auth)
                self.assertEqual(rated.status_code, 200)
                current = rated.json()
                self.assertEqual(current["prompt"]["phase"], "reflection")
                answered = self.client.post(url, data=json.dumps({
                    "expected_version": current["turn_version"], "item_id": item_id,
                    "kind": "text", "text": f"For {item_id}, I check my thinking and task context.",
                }), content_type="application/json", **auth)
                self.assertEqual(answered.status_code, 200)
                current = answered.json()
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["prompt"]["phase"], "complete")
        self.assertEqual(set(current["results"]), set(item_ids))
        session = ResponseSession.objects.get(public_id=started["session_id"])
        self.assertIsNone(session.completion_snapshot)
        finalized = self.client.post(self.base + f"sessions/{started['session_id']}/finalize/", data=json.dumps({
            "expected_version": current["turn_version"],
        }), content_type="application/json", **auth)
        self.assertEqual(finalized.status_code, 200)
        self.assertEqual(finalized.json()["status"], "completed")
        session.refresh_from_db()
        self.assertEqual(set(session.completion_snapshot["results"]), set(item_ids))
        self.assertEqual(ResponseMessage.objects.filter(response_session=session, role="student").count(), 22)

    def test_wrong_item_never_calls_assessor_or_persists_a_turn(self):
        conversational = self.make_occurrence(compiled_protocol=json.loads((ROOT / "ulia_conversational.json").read_text()))
        base = f"{API}{conversational.public_id}/"
        started = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        url = base + f"sessions/{started['session_id']}/turns/"
        auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        with patch("leai.api.student_responses.assess_text") as assessor:
            response = self.client.post(url, data=json.dumps({
                "expected_version": 1, "item_id": "P2", "kind": "text", "text": "Wrong item",
            }), content_type="application/json", **auth)
        self.assertEqual(response.status_code, 422)
        assessor.assert_not_called()
        self.assertEqual(ResponseMessage.objects.filter(response_session__public_id=started["session_id"], role="student").count(), 0)

    def test_team_survey_cannot_start_without_team_selection(self):
        team = self.make_occurrence(audience="team", compiled_protocol=self.occurrence.revision.compiled_protocol)
        response = self.client.post(f"{API}{team.public_id}/sessions/", data=CONSENT, content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_all_eleven_conversational_questions_complete_in_exact_order(self):
        protocol = json.loads((ROOT / "ulia_conversational.json").read_text())
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        base = f"{API}{occurrence.public_id}/"
        current = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {current['token']}"}
        item_ids = [item["id"] for section in protocol["sections"] for item in section["items"]]
        with patch("leai.api.student_responses.assess_text", side_effect=lambda protocol, state, text: {
            "sufficient": True, "followup": "", "evidence_for": [item_ids[state["item_index"]]],
            "covered_targets": ["decision_process"] if item_ids[state["item_index"]] == "P1" else [],
        }):
            for section in protocol["sections"]:
                for item in section["items"]:
                    self.assertEqual(current["prompt"]["text"], item["prompt"])
                    self.assertEqual(current["prompt"]["wording"], "exact")
                    response = self.client.post(base + f"sessions/{current['session_id']}/turns/", data=json.dumps({
                        "expected_version": current["turn_version"], "item_id": item["id"],
                        "kind": "text", "text": f"For {item['id']}, I compare AI suggestions to my task.",
                    }), content_type="application/json", **auth)
                    self.assertEqual(response.status_code, 200)
                    current = response.json()
        self.assertEqual(current["status"], "active")
        self.assertEqual(set(current["results"]), set(item_ids))

    def test_assessment_outage_does_not_save_student_answer_and_can_retry(self):
        protocol = json.loads((ROOT / "ulia_conversational.json").read_text())
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        base = f"{API}{occurrence.public_id}/"
        started = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        url = base + f"sessions/{started['session_id']}/turns/"
        body = json.dumps({"expected_version": 1, "item_id": "P1", "kind": "text", "text": "I remove private data."})
        with patch("leai.api.student_responses.assess_text", side_effect=AssessmentError("unavailable")):
            failed = self.client.post(url, data=body, content_type="application/json", **auth)
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(ResponseMessage.objects.filter(response_session__public_id=started["session_id"], role="student").count(), 0)
        with patch("leai.api.student_responses.assess_text", return_value={
            "sufficient": True, "followup": "", "evidence_for": ["P1"], "covered_targets": ["decision_process"],
        }):
            retried = self.client.post(url, data=body, content_type="application/json", **auth)
        self.assertEqual(retried.status_code, 200)

    def test_explicit_i_dont_know_declines_current_item_without_model_call(self):
        protocol = json.loads((ROOT / "ulia_conversational.json").read_text())
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        base = f"{API}{occurrence.public_id}/"
        started = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        with patch("leai.api.student_responses.assess_text") as assessor:
            response = self.client.post(base + f"sessions/{started['session_id']}/turns/", data=json.dumps({
                "expected_version": 1, "item_id": "P1", "kind": "text", "text": "I don't know.",
            }), content_type="application/json", **auth)
        self.assertEqual(response.status_code, 200)
        assessor.assert_not_called()
        self.assertEqual(response.json()["results"]["P1"]["status"], "declined")
        self.assertEqual(response.json()["prompt"]["item_id"], "P2")
        saved = ResponseMessage.objects.get(response_session__public_id=started["session_id"], role="student")
        self.assertEqual(saved.content, "I don't know.")

    def test_answer_can_be_remapped_after_last_question_until_final_download(self):
        protocol = json.loads((ROOT / "ulia_conversational.json").read_text())
        protocol["sections"] = [{**protocol["sections"][0], "items": protocol["sections"][0]["items"][:1]}]
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        base = f"{API}{occurrence.public_id}/"
        started = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        session_url = base + f"sessions/{started['session_id']}/"
        auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        with patch("leai.api.student_responses.assess_text", return_value={
            "sufficient": True, "followup": "", "evidence_for": ["P1"],
            "covered_targets": ["decision_process"],
        }):
            first = self.client.post(session_url + "turns/", data=json.dumps({
                "expected_version": 1, "item_id": "P1", "kind": "text",
                "text": "I choose a short task description for the AI.",
            }), content_type="application/json", **auth)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["prompt"]["phase"], "complete")
        self.assertEqual(first.json()["status"], "active")
        self.assertEqual(first.json()["answer_map"]["P1"], [3])

        with patch("leai.api.student_responses.classify_revision", return_value={
            "item_id": "P1", "operation": "replace",
            "answer_text": "I now remove private details before sharing task context.",
        }), patch("leai.api.student_responses.assess_text", return_value={
            "sufficient": True, "followup": "", "evidence_for": ["P1"],
            "covered_targets": ["decision_process"],
        }):
            revised = self.client.post(session_url + "turns/", data=json.dumps({
                "expected_version": first.json()["turn_version"], "kind": "text",
                "text": "Actually, change P1: I now remove private details before sharing task context.",
            }), content_type="application/json", **auth)
        self.assertEqual(revised.status_code, 200)
        self.assertEqual(revised.json()["status"], "active")
        self.assertEqual(revised.json()["answer_map"]["P1"], [5])
        self.assertEqual(revised.json()["messages"][-2]["attribution"]["operation"], "replace")
        self.assertEqual(self.client.get(session_url, **auth).json()["answer_map"]["P1"], [5])

        frozen = self.client.post(session_url + "finalize/", data=json.dumps({
            "expected_version": revised.json()["turn_version"],
        }), content_type="application/json", **auth)
        self.assertEqual(frozen.status_code, 200)
        self.assertEqual(frozen.json()["status"], "completed")
        session = ResponseSession.objects.get(public_id=started["session_id"])
        self.assertEqual(session.completion_snapshot["answer_map"]["P1"], [5])
        self.assertEqual(self.client.post(session_url + "turns/", data=json.dumps({
            "expected_version": frozen.json()["turn_version"], "kind": "text", "text": "revise P1 again",
        }), content_type="application/json", **auth).status_code, 409)

    def test_student_can_add_to_p1_while_p2_is_current_without_advancing_p2(self):
        protocol = json.loads((ROOT / "ulia_conversational.json").read_text())
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        base = f"{API}{occurrence.public_id}/"
        started = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        url = base + f"sessions/{started['session_id']}/turns/"
        auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        skipped = self.client.post(url, data=json.dumps({
            "expected_version": 1, "item_id": "P1", "kind": "text", "text": "I don't know",
        }), content_type="application/json", **auth).json()
        self.assertEqual(skipped["prompt"]["item_id"], "P2")
        with patch("leai.api.student_responses.classify_revision", return_value={
            "item_id": "P1", "operation": "add", "answer_text": "I choose useful task context.",
        }), patch("leai.api.student_responses.assess_text", return_value={
            "sufficient": True, "followup": "", "evidence_for": ["P1"],
            "covered_targets": ["decision_process"],
        }):
            revised = self.client.post(url, data=json.dumps({
                "expected_version": skipped["turn_version"], "item_id": "P2", "kind": "text",
                "text": "Actually, add to my P1 answer: I choose useful task context.",
            }), content_type="application/json", **auth)
        self.assertEqual(revised.status_code, 200)
        self.assertEqual(revised.json()["prompt"]["item_id"], "P2")
        self.assertEqual(revised.json()["answer_map"]["P1"], [3, 5])
        self.assertEqual(revised.json()["results"]["P1"]["status"], "answered")
        self.assertEqual(revised.json()["messages"][-2]["attribution"]["item_id"], "P1")
        reply = revised.json()["messages"][-1]["content"]
        self.assertNotIn("P1", reply)
        self.assertIn("information the AI needs to perform the task", reply)

    def test_clarification_request_explains_current_question_without_advancing_or_mapping_an_answer(self):
        protocol = json.loads((ROOT / "ulia_conversational.json").read_text())
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        base = f"{API}{occurrence.public_id}/"
        started = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        url = base + f"sessions/{started['session_id']}/turns/"
        auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        first = self.client.post(url, data=json.dumps({
            "expected_version": 1, "item_id": "P1", "kind": "skip",
        }), content_type="application/json", **auth).json()
        clarification = "By information the AI needs, I mean details that help it complete a task, such as the goal, audience, or constraints. What information would matter for one of your assignments?"
        with patch("leai.api.student_responses.classify_revision") as classify, patch(
            "leai.api.student_responses.assess_text", return_value={
            "intent": "clarification", "clarification_response": clarification,
            "sufficient": False, "followup": "", "evidence_for": [], "covered_targets": [],
        }) as assessor:
            response = self.client.post(url, data=json.dumps({
                "expected_version": first["turn_version"], "item_id": "P2", "kind": "text",
                "text": "What do you mean by question 2? Can you give an example?",
            }), content_type="application/json", **auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["prompt"]["item_id"], "P2")
        self.assertEqual(response.json()["prompt"]["phase"], "answer")
        self.assertNotIn("P2", response.json()["answer_map"])
        self.assertNotIn("P2", response.json()["results"])
        self.assertEqual(response.json()["messages"][-1]["content"], clarification)
        saved = ResponseMessage.objects.filter(response_session__public_id=started["session_id"], role="student").latest("sequence")
        self.assertEqual(saved.attribution["kind"], "clarification")
        classify.assert_not_called()
        assessor.assert_called_once()

    def test_incomplete_revision_reasks_that_item_then_returns_to_the_current_question(self):
        protocol = json.loads((ROOT / "ulia_conversational.json").read_text())
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        base = f"{API}{occurrence.public_id}/"
        started = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        url = base + f"sessions/{started['session_id']}/turns/"
        auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        skipped = self.client.post(url, data=json.dumps({
            "expected_version": 1, "item_id": "P1", "kind": "skip",
        }), content_type="application/json", **auth).json()
        with patch("leai.api.student_responses.classify_revision", return_value={
            "item_id": "P1", "operation": "replace", "answer_text": "I consider the task goal.",
        }), patch("leai.api.student_responses.assess_text", return_value={
            "sufficient": False, "followup": "How do you decide what information to share?",
            "evidence_for": ["P1"], "covered_targets": [],
        }):
            revised = self.client.post(url, data=json.dumps({
                "expected_version": skipped["turn_version"], "item_id": "P2", "kind": "text",
                "text": "Actually, change my P1 answer: I consider the task goal.",
            }), content_type="application/json", **auth)

        self.assertEqual(revised.status_code, 200)
        self.assertEqual(revised.json()["prompt"]["item_id"], "P1")
        self.assertEqual(revised.json()["prompt"]["phase"], "probe")
        self.assertEqual(revised.json()["prompt"]["text"], "How do you decide what information to share?")
        self.assertNotIn("P1", revised.json()["messages"][-1]["content"])

        with patch("leai.api.student_responses.assess_text", return_value={
            "sufficient": True, "followup": "", "evidence_for": ["P1"],
            "covered_targets": ["decision_process"],
        }):
            answered = self.client.post(url, data=json.dumps({
                "expected_version": revised.json()["turn_version"], "item_id": "P1", "kind": "text",
                "text": "I leave out private details and share only what the task needs.",
            }), content_type="application/json", **auth)
        self.assertEqual(answered.status_code, 200)
        self.assertEqual(answered.json()["prompt"]["item_id"], "P2")
        self.assertEqual(answered.json()["prompt"]["phase"], "answer")

    def test_normal_answer_starting_actually_still_answers_current_question(self):
        protocol = json.loads((ROOT / "ulia_conversational.json").read_text())
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        base = f"{API}{occurrence.public_id}/"
        started = self.client.post(base + "sessions/", data=CONSENT, content_type="application/json").json()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {started['token']}"}
        with patch("leai.api.student_responses.classify_revision", return_value={
            "item_id": "", "operation": "answer", "answer_text": "", "clarification": "",
        }), patch("leai.api.student_responses.assess_text", return_value={
            "sufficient": True, "followup": "", "evidence_for": ["P1"],
            "covered_targets": ["decision_process"],
        }):
            response = self.client.post(base + f"sessions/{started['session_id']}/turns/", data=json.dumps({
                "expected_version": 1, "item_id": "P1", "kind": "text",
                "text": "Actually, I choose context based on the task.",
            }), content_type="application/json", **auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["prompt"]["item_id"], "P2")
