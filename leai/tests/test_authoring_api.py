import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from leai.models import (
    Course, CourseMembership, Institution, InstitutionMembership,
    InstructorAccount, PreviewMessage, QuestionSet, QuestionSetRevision,
    ResponseMessage, ResponseSession, SurveyOccurrence, AuthoringMessage, QuestionSetDraft,
)
from leai.tests.session_client import SessionClient


ROOT = "/datapipeline/api/v1/"


@override_settings(ROOT_URLCONF="leai.tests.feedback_chat_urls")
class AuthoringApiTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.client = SessionClient()
        institution = Institution.objects.create(slug="wizard-test", name="Wizard Test")
        self.course = Course.objects.create(institution=institution, course_code="course", name="Course")
        self.other_course = Course.objects.create(institution=institution, course_code="other", name="Other")
        user = get_user_model().objects.create_user(
            username="wizard-teacher", email="wizard@example.edu", password="Test-Password-Only-2026!",
        )
        self.actor = InstructorAccount.objects.create(
            user=user, email=user.email, display_name="Wizard Teacher",
        )
        member = InstitutionMembership.objects.create(account=self.actor, institution=institution, role="instructor")
        CourseMembership.objects.create(course=self.course, institution_membership=member, role="owner")
        login = self.client.post(ROOT + "instructor_sessions/", json.dumps({
            "email": user.email, "password": "Test-Password-Only-2026!",
        }), content_type="application/json")
        self.assertEqual(login.status_code, 201)

    def url(self, suffix, *, course=None):
        return ROOT + f"instructor_courses/{(course or self.course).public_id}/{suffix}"

    def post(self, suffix, data, key="wizard-key-1234"):
        return self.client.post(self.url(suffix), json.dumps(data), content_type="application/json", HTTP_IDEMPOTENCY_KEY=key)

    def patch(self, suffix, data, key="wizard-save-1234"):
        return self.client.patch(self.url(suffix), json.dumps(data), content_type="application/json", HTTP_IDEMPOTENCY_KEY=key)

    def create(self):
        return self.post("question-sets/", {
            "title": "My reflection", "audience": "individual",
            "collection_style": "guided", "template_id": "weekly-reflection",
        })

    def test_create_save_restore_freeze_preview_publish_is_idempotent(self):
        first = self.create()
        self.assertEqual(first.status_code, 201, first.content)
        replay = self.create()
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json()["id"], first.json()["id"])
        self.assertEqual(QuestionSet.objects.count(), 1)
        question_set_id = first.json()["id"]
        body = first.json()["body"]
        body["sections"][0]["items"][0]["prompt"] = "What did you learn this week?"
        draft_path = f"question-sets/{question_set_id}/draft/"
        saved = self.patch(draft_path, {"expected_version": 1, "body": body})
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["draft_version"], 2)
        self.assertEqual(self.patch(draft_path, {"expected_version": 1, "body": body}, "another-key-1234").status_code, 409)
        self.assertEqual(len(self.client.get(self.url(f"question-sets/{question_set_id}/versions/")).json()["versions"]), 2)
        frozen = self.post(f"question-sets/{question_set_id}/freeze/", {"expected_version": 2})
        self.assertEqual(frozen.status_code, 200, frozen.content)
        revision_id = frozen.json()["revision"]["id"]
        self.assertEqual(self.post(f"question-sets/{question_set_id}/freeze/", {"expected_version": 2}).json()["revision"]["id"], revision_id)
        self.assertEqual(QuestionSetRevision.objects.count(), 1)
        self.assertEqual(PreviewMessage.objects.count(), 0)
        skipped = self.post(f"revisions/{revision_id}/preview-decision/", {"decision": "skipped"}, "preview-key-1234")
        self.assertEqual(skipped.status_code, 200, skipped.content)
        publish = self.post(f"revisions/{revision_id}/publish/", {"label": "Week 1", "opens_at": None, "closes_at": None}, "publish-key-1234")
        self.assertEqual(publish.status_code, 201, publish.content)
        again = self.post(f"revisions/{revision_id}/publish/", {"label": "Week 1", "opens_at": None, "closes_at": None}, "publish-key-1234")
        self.assertEqual(again.json(), publish.json())
        self.assertEqual(SurveyOccurrence.objects.count(), 1)
        self.assertEqual(ResponseSession.objects.count(), 0)
        self.assertEqual(ResponseMessage.objects.count(), 0)

    def test_preview_completion_is_isolated_and_needs_exact_questions(self):
        question_set_id = self.create().json()["id"]
        frozen = self.post(f"question-sets/{question_set_id}/freeze/", {"expected_version": 1})
        revision_id = frozen.json()["revision"]["id"]
        started = self.post(f"revisions/{revision_id}/preview/", {})
        self.assertEqual(started.status_code, 200, started.content)
        preview_id = started.json()["preview_id"]
        premature = self.post(f"revisions/{revision_id}/preview-decision/", {"decision": "completed"}, "decision-key-1234")
        self.assertEqual(premature.status_code, 409)
        items = [item for section in frozen.json()["revision"]["body"]["sections"] for item in section["items"]]
        for item in items:
            answer = self.post(f"previews/{preview_id}/messages/", {"item_id": item["id"], "content": "A preview answer."})
            self.assertEqual(answer.status_code, 201, answer.content)
        complete = self.post(f"revisions/{revision_id}/preview-decision/", {"decision": "completed"}, "decision-key-1234")
        self.assertEqual(complete.status_code, 200, complete.content)
        self.assertEqual(PreviewMessage.objects.count(), 2 * len(items) + 2)
        self.assertEqual(ResponseSession.objects.count(), 0)

    def test_course_access_and_unsupported_team_open_fail_closed(self):
        self.assertEqual(SessionClient().get(self.url("question-sets/")).status_code, 401)
        self.assertEqual(self.client.get(self.url("question-sets/", course=self.other_course)).status_code, 404)
        team_open = self.post("question-sets/", {"title": "Team", "audience": "team", "collection_style": "open"})
        self.assertEqual(team_open.status_code, 400)
        self.assertEqual(QuestionSet.objects.count(), 0)

    def test_ai_run_queues_only_ids_and_rejects_stale_base(self):
        question_set_id = self.create().json()["id"]
        path = f"question-sets/{question_set_id}/ai-runs/"
        with self.captureOnCommitCallbacks(execute=False):
            response = self.post(path, {"content": "Make the first question shorter.", "expected_version": 1}, "ai-key-1234")
        self.assertEqual(response.status_code, 202, response.content)
        from leai.models.jobs import DomainJob
        job = DomainJob.objects.get(public_id=response.json()["job_id"])
        self.assertEqual(job.job_type, "authoring_ai_run")
        self.assertEqual(set(job.payload), {"authoring_run_id"})
        self.assertNotIn("Make the first", str(job.payload))
        self.assertEqual(self.client.get(self.url(f"jobs/{job.public_id}/")).status_code, 200)
        stale = self.post(path, {"content": "Another instruction", "expected_version": 2}, "ai-key-5678")
        self.assertEqual(stale.status_code, 409)

    def test_ai_tool_result_updates_only_matching_draft_version(self):
        question_set_id = self.create().json()["id"]
        path = f"question-sets/{question_set_id}/ai-runs/"
        with self.captureOnCommitCallbacks(execute=False):
            response = self.post(path, {"content": "Shorten the first question.", "expected_version": 1}, "ai-matching-key")
        from leai.models.jobs import DomainJob
        from leai.services.jobs import claim_domain_job
        from leai.services.authoring_orchestrator import process_authoring_ai_job
        job = claim_domain_job(response.json()["job_id"])
        candidate = self.client.get(self.url(f"question-sets/{question_set_id}/draft/")).json()["body"]
        candidate["sections"][0]["items"][0]["prompt"] = "What stood out this week?"
        with patch("leai.services.authoring_orchestrator.run_authoring_orchestrator", return_value=(candidate, "I shortened the first question.")):
            self.assertTrue(process_authoring_ai_job(job))
        self.assertEqual(DomainJob.objects.get(pk=job.pk).status, "completed")
        self.assertEqual(QuestionSetDraft.objects.get(question_set__public_id=question_set_id).current_version, 2)
        self.assertEqual(AuthoringMessage.objects.filter(role="assistant").count(), 1)

        with self.captureOnCommitCallbacks(execute=False):
            second = self.post(path, {"content": "Change another question.", "expected_version": 2}, "ai-stale-key-2")
        job = claim_domain_job(second.json()["job_id"])
        changed = self.client.get(self.url(f"question-sets/{question_set_id}/draft/")).json()["body"]
        changed["intro"] = "New manual introduction."
        self.assertEqual(self.patch(f"question-sets/{question_set_id}/draft/", {"expected_version": 2, "body": changed}, "manual-after-ai").status_code, 200)
        with patch("leai.services.authoring_orchestrator.run_authoring_orchestrator", return_value=(candidate, "I changed the question.")):
            self.assertFalse(process_authoring_ai_job(job))
        self.assertEqual(DomainJob.objects.get(pk=job.pk).error_code, "stale_draft")
        self.assertEqual(QuestionSetDraft.objects.get(question_set__public_id=question_set_id).canonical_body["intro"], "New manual introduction.")

    def test_team_setup_after_publication_enables_self_selected_student_session(self):
        created = self.post("question-sets/", {
            "title": "Team reflection", "audience": "team", "collection_style": "guided",
        }, "create-team-1234")
        self.assertEqual(created.status_code, 201, created.content)
        question_set_id = created.json()["id"]
        revision_id = self.post(f"question-sets/{question_set_id}/freeze/", {"expected_version": 1}).json()["revision"]["id"]
        self.assertEqual(self.post(f"revisions/{revision_id}/preview-decision/", {"decision": "skipped"}, "skip-team-1234").status_code, 200)
        published = self.post(f"revisions/{revision_id}/publish/", {
            "label": "Team week 1", "completion_certificate_enabled": True,
            "completed_response_download_enabled": True,
        }, "publish-team-1234")
        self.assertEqual(published.status_code, 201, published.content)
        survey_id = published.json()["id"]
        student_url = ROOT + f"surveys/{survey_id}/"
        self.assertTrue(self.client.get(student_url).json()["team_setup_required"])
        self.assertFalse(self.client.get(student_url).json()["available"])
        setup_path = f"surveys/{survey_id}/teams/"
        ready = self.post(setup_path, {"labels": ["Team A", "Team B"]}, "setup-team-1234")
        self.assertEqual(ready.status_code, 200, ready.content)
        self.assertFalse(ready.json()["team_setup_required"])
        self.assertEqual(self.post(setup_path, {"labels": ["Team A", "Team B"]}, "setup-team-1234").status_code, 200)
        student = self.client.get(student_url).json()
        self.assertTrue(student["available"])
        self.assertEqual([choice["label"] for choice in student["team_choices"]], ["Team A", "Team B"])
        self.assertTrue(student["completion_certificate_enabled"])
        sessions_url = ROOT + f"surveys/{survey_id}/sessions/"
        consent = {"terms_consent": True, "research_consent": False}
        self.assertEqual(self.client.post(sessions_url, json.dumps(consent), content_type="application/json").status_code, 400)
        selected = {**consent, "team_snapshot_item_id": student["team_choices"][0]["id"]}
        started = self.client.post(sessions_url, json.dumps(selected), content_type="application/json")
        self.assertEqual(started.status_code, 201, started.content)
        self.assertEqual(ResponseSession.objects.get(public_id=started.json()["session_id"]).team_snapshot_item.label, "Team A")
