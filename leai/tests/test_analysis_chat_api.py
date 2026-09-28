import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from leai.models import AnalysisChatMessage, AnalysisChatSession, AnalysisCitation, AnalysisScopeOccurrence, CourseMembership, InstitutionMembership, ResponseMessage
from leai.models.jobs import DomainJob
from leai.services.analysis_chat import process_feedback_chat_job
from leai.services.jobs import claim_domain_job, enqueue_domain_job
from leai.tests.session_client import SessionClient
from leai.tests.test_response_models import ResponseFixturesMixin


ROOT = "/datapipeline/api/v1/"


@override_settings(ROOT_URLCONF="leai.tests.feedback_chat_urls")
class FeedbackChatApiTests(ResponseFixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = SessionClient()
        self.account.must_change_password = False
        self.account.save(update_fields=["must_change_password"])
        self.account.user.set_password("Test-Password-Only-2026!")
        self.account.user.save(update_fields=["password"])
        self.membership = InstitutionMembership.objects.create(account=self.account, institution=self.institution, role="instructor")
        self.course_membership = CourseMembership.objects.create(course=self.course, institution_membership=self.membership, role="instructor")
        login = self.client.post(ROOT + "instructor_sessions/", data=json.dumps({"email": self.account.email, "password": "Test-Password-Only-2026!"}), content_type="application/json")
        self.assertEqual(login.status_code, 201)

    def route(self, suffix, course=None):
        return ROOT + f"instructor_courses/{(course or self.course).public_id}/analysis/{suffix}"

    def job_route(self, job_id, course=None):
        return ROOT + f"instructor_courses/{(course or self.course).public_id}/jobs/{job_id}/"

    def create_chat(self):
        return self.client.post(self.route("chats/"), data=json.dumps({}), content_type="application/json")

    def test_occurrence_picker_is_course_scoped_and_requires_auth(self):
        occurrence = self.make_occurrence()
        path = self.route("occurrences/")
        self.assertEqual(SessionClient().get(path).status_code, 401)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json()["occurrences"][0]["id"], str(occurrence.public_id))
        self.assertEqual(self.client.get(self.route("occurrences/", self.other_course)).status_code, 404)

    def test_running_job_status_is_available_while_web_thread_is_active(self):
        job = enqueue_domain_job(
            job_type="feedback_chat_turn",
            course=self.course,
            actor=self.account,
            payload={"user_message_id": "1", "occurrence_ids": []},
        )
        self.assertIsNotNone(claim_domain_job(job.public_id))

        response = self.client.get(self.job_route(job.public_id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "running")

    def test_add_source_and_idempotent_turn_snapshot_only_ids(self):
        first = self.make_occurrence()
        second = self.make_occurrence()
        chat_response = self.create_chat()
        self.assertEqual(chat_response.status_code, 201)
        chat_id = chat_response.json()["id"]
        scope_url = self.route(f"chats/{chat_id}/scope/")
        added = self.client.post(scope_url, data=json.dumps({"occurrence_ids": [str(first.public_id)]}), content_type="application/json")
        self.assertEqual(added.status_code, 200)
        turns_url = self.route(f"chats/{chat_id}/turns/")
        headers = {"HTTP_IDEMPOTENCY_KEY": "chat-turn-one"}
        body = json.dumps({"content": "What feedback themes appear?"})
        turn = self.client.post(turns_url, data=body, content_type="application/json", **headers)
        replay = self.client.post(turns_url, data=body, content_type="application/json", **headers)
        self.assertEqual(turn.status_code, 202)
        self.assertEqual(replay.json(), turn.json())
        self.assertEqual(AnalysisChatMessage.objects.filter(analysis_chat_session__public_id=chat_id, role="user").count(), 1)
        self.assertEqual(AnalysisChatSession.objects.get(public_id=chat_id).title, "What feedback themes appear?")
        blocked = self.client.post(turns_url, data=json.dumps({"content": "Do this before turn one finishes."}), content_type="application/json", HTTP_IDEMPOTENCY_KEY="chat-turn-too-soon")
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json(), {"error": "turn_in_progress"})
        job = DomainJob.objects.get(public_id=turn.json()["job_id"])
        self.assertEqual(job.payload, {"user_message_id": job.payload["user_message_id"], "occurrence_ids": [str(first.public_id)]})
        self.assertNotIn("What feedback", str(job.payload))
        self.assertEqual(AnalysisScopeOccurrence.objects.filter(analysis_chat_session__public_id=chat_id).count(), 1)
        job_status = self.client.get(self.job_route(job.public_id))
        self.assertEqual(job_status.status_code, 200)
        self.assertEqual(job_status.json()["status"], "pending")
        self.assertNotIn("payload", job_status.json())
        self.assertNotIn("What feedback", str(job_status.json()))

        pending_messages = self.client.get(self.route(f"chats/{chat_id}/")).json()["messages"]
        pending_scope = self.client.post(
            scope_url,
            data=json.dumps({"occurrence_ids": [str(second.public_id)]}),
            content_type="application/json",
        )
        self.assertEqual(pending_scope.status_code, 200)
        self.assertEqual(pending_scope.json()["sources"], [
            {"id": str(first.public_id), "label": first.label, "revision": first.revision.revision_number},
            {"id": str(second.public_id), "label": second.label, "revision": second.revision.revision_number},
        ])
        pending_chat = self.client.get(self.route(f"chats/{chat_id}/")).json()
        self.assertEqual(pending_chat["messages"], pending_messages)
        self.assertEqual(job.payload["occurrence_ids"], [str(first.public_id)])
        self.assertEqual(self.client.get(self.job_route(job.public_id)).json()["status"], "pending")

        response_session = self.make_student_session(occurrence=first, status="completed", completed_at=timezone.now(), completion_snapshot={"completed_at": timezone.now().isoformat()})
        evidence = ResponseMessage.objects.create(response_session=response_session, sequence=1, role="student", input_method="typed", content="The weekly instructions were confusing.")
        chat = AnalysisChatSession.objects.get(public_id=chat_id)
        assistant = AnalysisChatMessage.objects.create(analysis_chat_session=chat, sequence=2, role="assistant", content="Students found the instructions unclear.")
        AnalysisCitation.objects.create(analysis_chat_message=assistant, response_message=evidence, claim_key="clarity", evidence_quote="The weekly instructions were confusing.")
        job.status = "completed"
        job.result = {"assistant_message_id": str(assistant.pk)}
        job.completed_at = timezone.now()
        job.save(update_fields=("status", "result", "completed_at", "updated_at"))
        old_messages = self.client.get(self.route(f"chats/{chat_id}/")).json()["messages"]
        self.client.post(scope_url, data=json.dumps({"occurrence_ids": [str(second.public_id)]}), content_type="application/json")
        after_scope = self.client.get(self.route(f"chats/{chat_id}/")).json()
        self.assertEqual(after_scope["messages"], old_messages)
        self.assertEqual(len(after_scope["messages"][1]["citations"]), 1)
        self.assertEqual(len(after_scope["sources"]), 2)
        second_turn = self.client.post(turns_url, data=json.dumps({"content": "Compare another week."}), content_type="application/json", HTTP_IDEMPOTENCY_KEY="chat-turn-two")
        self.assertEqual(second_turn.status_code, 202)
        self.assertEqual(DomainJob.objects.get(public_id=turn.json()["job_id"]).payload["occurrence_ids"], [str(first.public_id)])
        self.assertEqual(DomainJob.objects.get(public_id=second_turn.json()["job_id"]).payload["occurrence_ids"], [str(first.public_id), str(second.public_id)])

    def test_foreign_scope_and_idempotency_conflict_fail_closed(self):
        chat_id = self.create_chat().json()["id"]
        scope_url = self.route(f"chats/{chat_id}/scope/")
        foreign = self.make_occurrence(course=self.other_course)
        response = self.client.post(scope_url, data=json.dumps({"occurrence_ids": [str(foreign.public_id)]}), content_type="application/json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(AnalysisScopeOccurrence.objects.count(), 0)
        local = self.make_occurrence()
        self.client.post(scope_url, data=json.dumps({"occurrence_ids": [str(local.public_id)]}), content_type="application/json")
        turns_url = self.route(f"chats/{chat_id}/turns/")
        key = {"HTTP_IDEMPOTENCY_KEY": "same-key"}
        self.assertEqual(self.client.post(turns_url, data=json.dumps({"content": "Question one"}), content_type="application/json", **key).status_code, 202)
        conflict = self.client.post(turns_url, data=json.dumps({"content": "Different question"}), content_type="application/json", **key)
        self.assertEqual(conflict.status_code, 409)

    def test_chat_turn_starts_background_execution_after_the_request_commits(self):
        occurrence = self.make_occurrence()
        chat_id = self.create_chat().json()["id"]
        self.client.post(
            self.route(f"chats/{chat_id}/scope/"),
            data=json.dumps({"occurrence_ids": [str(occurrence.public_id)]}),
            content_type="application/json",
        )
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.post(
                self.route(f"chats/{chat_id}/turns/"),
                data=json.dumps({"content": "Summarize the feedback."}),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="thread-backed-turn",
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(callbacks), 1)
        with patch("leai.api.feedback_chat.start_domain_job_thread") as start_job:
            callbacks[0]()
        start_job.assert_called_once_with(response.json()["job_id"])

    def test_chat_access_is_rechecked_after_membership_revocation(self):
        chat_id = self.create_chat().json()["id"]
        CourseMembership.objects.filter(pk=self.course_membership.pk).delete()
        response = self.client.get(self.route(f"chats/{chat_id}/"))
        self.assertEqual(response.status_code, 404)

    def test_background_turn_persists_only_exact_canonical_response_citations(self):
        occurrence = self.make_occurrence()
        response_session = self.make_student_session(occurrence=occurrence, status="completed", completed_at=timezone.now(), completion_snapshot={"completed_at": timezone.now().isoformat()})
        evidence = ResponseMessage.objects.create(response_session=response_session, sequence=1, role="student", input_method="typed", content="The directions were hard to follow.")
        chat = AnalysisChatSession.objects.create(course=self.course, actor_account=self.account, origin_surface="feedback_chat")
        AnalysisScopeOccurrence.objects.create(analysis_chat_session=chat, survey_occurrence=occurrence)
        user_message = AnalysisChatMessage.objects.create(analysis_chat_session=chat, sequence=1, role="user", input_method="typed", content="What should I clarify?")
        enqueue_domain_job(job_type="feedback_chat_turn", course=self.course, actor=self.account, payload={"user_message_id": str(user_message.pk), "occurrence_ids": [str(occurrence.public_id)]})
        job = claim_domain_job(DomainJob.objects.get(payload__user_message_id=str(user_message.pk)).public_id)
        with patch("datapipeline.openai_client.run_structured", return_value={"parsed": {
            "answer": "Students need clearer directions.",
            "citations": [{"source_message_id": evidence.pk, "claim_key": "clarity", "evidence_quote": "hard to follow"}],
        }}) as provider:
            self.assertTrue(process_feedback_chat_job(job))
        self.assertNotIn("temperature", provider.call_args.kwargs)
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        assistant = AnalysisChatMessage.objects.get(analysis_chat_session=chat, role="assistant")
        citation = AnalysisCitation.objects.get(analysis_chat_message=assistant)
        self.assertEqual(citation.response_message_id, evidence.pk)
        self.assertEqual(citation.evidence_quote, "hard to follow")
        self.assertIsNone(citation.response_session_id)

    def test_background_turn_rejects_quote_not_present_in_the_canonical_source(self):
        occurrence = self.make_occurrence()
        response_session = self.make_student_session(occurrence=occurrence, status="completed", completed_at=timezone.now(), completion_snapshot={"completed_at": timezone.now().isoformat()})
        evidence = ResponseMessage.objects.create(response_session=response_session, sequence=1, role="student", input_method="typed", content="The directions were hard to follow.")
        chat = AnalysisChatSession.objects.create(course=self.course, actor_account=self.account, origin_surface="feedback_chat")
        AnalysisScopeOccurrence.objects.create(analysis_chat_session=chat, survey_occurrence=occurrence)
        user_message = AnalysisChatMessage.objects.create(analysis_chat_session=chat, sequence=1, role="user", input_method="typed", content="What should I clarify?")
        enqueue_domain_job(job_type="feedback_chat_turn", course=self.course, actor=self.account, payload={"user_message_id": str(user_message.pk), "occurrence_ids": [str(occurrence.public_id)]})
        job = claim_domain_job(DomainJob.objects.get(payload__user_message_id=str(user_message.pk)).public_id)
        with patch("datapipeline.openai_client.run_structured", return_value={"parsed": {
            "answer": "Students need clearer directions.",
            "citations": [{"source_message_id": evidence.pk, "claim_key": "clarity", "evidence_quote": "The directions were perfectly clear."}],
        }}):
            self.assertFalse(process_feedback_chat_job(job))
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "turn_failed")
        self.assertFalse(AnalysisChatMessage.objects.filter(analysis_chat_session=chat, role="assistant").exists())
