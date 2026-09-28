import hashlib
import json

from django.test import TestCase, override_settings

from leai.models import (
    AuditEvent,
    CourseMembership,
    InstitutionMembership,
    PdfImportBatch,
    ResponseMessage,
    ResponseSession,
    ResponseSessionMatchSignal,
    SurveyOccurrence,
)
from leai.tests.session_client import SessionClient
from leai.tests.test_response_models import ResponseFixturesMixin


ROOT = "/datapipeline/api/v1/"


@override_settings(ROOT_URLCONF="leai.tests.feedback_analyzer_urls")
class FeedbackAnalyzerApiTests(ResponseFixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = SessionClient()
        self.account.must_change_password = False
        self.account.save(update_fields=["must_change_password"])
        self.account.user.set_password("Test-Password-Only-2026!")
        self.account.user.save(update_fields=["password"])
        self.membership = InstitutionMembership.objects.create(
            account=self.account,
            institution=self.institution,
            role="instructor",
        )
        CourseMembership.objects.create(
            course=self.course,
            institution_membership=self.membership,
            role="owner",
        )
        login = self.client.post(
            ROOT + "instructor_sessions/",
            data=json.dumps({
                "email": self.account.email,
                "password": "Test-Password-Only-2026!",
            }),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 201)

    def add_message(self, session, sequence, role, content, attribution=None):
        return ResponseMessage.objects.create(
            response_session=session,
            sequence=sequence,
            role=role,
            input_method="typed" if role == "student" else None,
            content=content,
            attribution=attribution or {},
        )

    def course_url(self, suffix, course=None):
        course = course or self.course
        return ROOT + f"instructor_courses/{course.public_id}/analysis/{suffix}/"

    def patch_settings(self, enabled, version=1):
        return self.client.patch(
            ROOT + f"instructor_courses/{self.course.public_id}/analysis-settings/",
            data=json.dumps({
                "anonymous_matching_enabled": enabled,
                "expected_settings_version": version,
            }),
            content_type="application/json",
        )

    def test_overview_requires_auth_and_rejects_foreign_occurrence_scope(self):
        occurrence = self.make_occurrence()
        self.make_student_session(occurrence=occurrence)
        foreign = self.make_occurrence(course=self.other_course)
        url = self.course_url("overview")

        self.assertEqual(
            SessionClient().get(url).status_code,
            401,
        )
        selected = self.client.get(url, {"occurrence_ids": str(occurrence.public_id)})
        rejected = self.client.get(url, {"occurrence_ids": str(foreign.public_id)})

        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json()["summary"]["response_count"], 0)
        self.assertFalse(selected.json()["occurrences"][0]["completion_certificate_enabled"])
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json(), {"error": "invalid_occurrence_scope"})

    def test_overview_counts_each_response_once_and_excludes_assistant_only_sessions(self):
        occurrence = self.make_occurrence()
        student = self.make_student_session(occurrence=occurrence)
        self.add_message(student, 1, "student", "A useful response has four words")
        self.add_message(student, 2, "student", "One more turn")
        self.add_message(student, 3, "assistant", "Assistant does not count")
        assistant_only = self.make_student_session(occurrence=occurrence)
        self.add_message(assistant_only, 1, "assistant", "Unanswered opening")
        batch = self.make_pdf_batch(occurrence=occurrence, status="completed")
        pdf = self.make_pdf_session(occurrence=occurrence, batch=batch)
        self.add_message(pdf, 1, "assistant", "Q: Prompt\n\nA: Imported answer")

        response = self.client.get(
            self.course_url("overview"),
            {"occurrence_ids": str(occurrence.public_id)},
        )

        self.assertEqual(response.status_code, 200)
        summary = response.json()["summary"]
        self.assertEqual(summary["response_count"], 2)
        self.assertEqual(summary["student_turn_count"], 2)
        self.assertEqual(summary["pdf_response_count"], 1)
        self.assertEqual(summary["participation"], {
            "state": "unavailable",
            "reason": "eligible_denominator_missing",
        })
        self.assertNotIn("assistant_only", response.content.decode())

    def test_response_pages_and_details_use_only_course_scoped_response_records(self):
        occurrence = self.make_occurrence()
        student = self.make_student_session(occurrence=occurrence)
        self.add_message(student, 1, "student", "Visible student answer")
        self.add_message(student, 2, "assistant", "Visible assistant prompt")
        foreign = self.make_occurrence(course=self.other_course)
        foreign_session = self.make_student_session(occurrence=foreign)
        self.add_message(foreign_session, 1, "student", "Foreign private answer")

        first_page = self.client.get(
            self.course_url("responses"),
            {"occurrence_ids": str(occurrence.public_id), "limit": "1"},
        )

        self.assertEqual(first_page.status_code, 200)
        page = first_page.json()
        self.assertEqual(len(page["results"]), 1)
        self.assertFalse(page["has_more"])
        detail = self.client.get(
            ROOT + f"instructor_courses/{self.course.public_id}/analysis/responses/{student.public_id}/"
        )
        denied = self.client.get(
            ROOT + f"instructor_courses/{self.course.public_id}/analysis/responses/{foreign_session.public_id}/"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(
            [message["content"] for message in detail.json()["transcript"]],
            ["Visible student answer"],
        )
        self.assertNotIn("capability", detail.content.decode())

    def test_response_pages_filter_ngram_terms_only_in_student_or_pdf_response_text(self):
        occurrence = self.make_occurrence()
        matched = self.make_student_session(occurrence=occurrence)
        self.add_message(matched, 1, "student", "Clear, instructions were helpful.")
        self.add_message(matched, 2, "assistant", "Clear instructions in assistant text do not count.")
        unmatched = self.make_student_session(occurrence=occurrence)
        self.add_message(unmatched, 1, "student", "The instructions were unclear.")
        pdf_batch = self.make_pdf_batch(occurrence=occurrence, status="completed")
        pdf = self.make_pdf_session(occurrence=occurrence, batch=pdf_batch)
        self.add_message(pdf, 1, "assistant", "Q: Reflection\n\nA: Clear instructions helped.")

        response = self.client.get(
            self.course_url("responses"),
            {"occurrence_ids": str(occurrence.public_id), "term": "clear instructions"},
        )

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual({row["response_id"] for row in results}, {str(matched.public_id), str(pdf.public_id)})

    def test_ngram_endpoint_uses_single_response_counts_and_same_schema_week_baseline(self):
        first = self.make_occurrence()
        second = SurveyOccurrence.objects.create(
            revision=first.revision,
            course=first.course,
            created_by=self.account,
            label="Week 2",
            provenance=first.provenance,
            management_mode=first.management_mode,
            settings_version=1,
        )
        first_session = self.make_student_session(occurrence=first)
        self.add_message(first_session, 1, "student", "Helpful peer review.")
        self.add_message(first_session, 2, "assistant", "Assistant wording is excluded.")
        target_session = self.make_student_session(occurrence=second)
        self.add_message(target_session, 1, "student", "Clear instructions helped.")
        pdf_batch = self.make_pdf_batch(occurrence=second, status="completed")
        pdf_session = self.make_pdf_session(occurrence=second, batch=pdf_batch)
        self.add_message(pdf_session, 1, "assistant", "Q: Reflection\n\nA: Clear instructions helped.")
        assistant_only = self.make_student_session(occurrence=second)
        self.add_message(assistant_only, 1, "assistant", "Assistant only response must not count.")

        response = self.client.get(
            self.course_url("ngrams"),
            {"occurrence_ids": str(second.public_id), "size": "2"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source_count"], 2)
        self.assertTrue(payload["keyness_available"])
        term = next(row for row in payload["items"] if row["term"] == "clear instructions")
        self.assertEqual(term["count"], 2)
        self.assertIsInstance(term["keyness"], float)
        self.assertNotIn("assistant wording", response.content.decode())
        self.assertNotIn("assistant only response", response.content.decode())

        all_weeks = self.client.get(self.course_url("ngrams"), {"size": "2"})
        self.assertEqual(all_weeks.status_code, 200)
        self.assertFalse(all_weeks.json()["keyness_available"])


    def test_settings_are_opt_in_versioned_and_audited_without_signal_values(self):
        url = ROOT + f"instructor_courses/{self.course.public_id}/analysis-settings/"
        initial = self.client.get(url)
        enabled = self.patch_settings(True)
        stale = self.patch_settings(False, version=1)

        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["anonymous_matching_enabled"])
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(enabled.json(), {
            "anonymous_matching_enabled": True,
            "settings_version": 2,
        })
        self.assertEqual(stale.status_code, 409)
        event = AuditEvent.objects.get(action="course.anonymous_matching.update")
        self.assertEqual(event.bounded_metadata, {
            "changed_fields": ["anonymous_matching_enabled"],
        })

    def test_matching_signals_store_only_keyed_digests_and_progress_uses_anonymous_labels(self):
        occurrence = self.make_occurrence()
        second_occurrence = SurveyOccurrence.objects.create(
            revision=occurrence.revision,
            course=occurrence.course,
            created_by=self.account,
            label="Week 2",
            provenance=occurrence.provenance,
            management_mode=occurrence.management_mode,
            settings_version=1,
        )
        token = "StudentSessionCapability_2026_Anonymous"
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        first = self.make_student_session(occurrence=occurrence, capability_digest=digest)
        self.add_message(first, 1, "student", "First response")
        second_token = "StudentSessionCapability_2026_Another"
        second = self.make_student_session(
            occurrence=second_occurrence,
            capability_digest=hashlib.sha256(second_token.encode("ascii")).hexdigest(),
        )
        self.add_message(second, 1, "student", "Second response")
        body = {"device_key": "legacy-device-key", "fingerprint": "visitor-fingerprint"}
        signal_url = ROOT + f"surveys/{occurrence.public_id}/sessions/{first.public_id}/matching-signals/"

        disabled = self.client.post(
            signal_url, data=json.dumps(body), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["accepted"])
        self.assertEqual(ResponseSessionMatchSignal.objects.count(), 0)

        self.assertEqual(self.patch_settings(True).status_code, 200)
        second_url = ROOT + f"surveys/{second_occurrence.public_id}/sessions/{second.public_id}/matching-signals/"
        for url, capability in ((signal_url, token), (second_url, second_token)):
            response = self.client.post(
                url, data=json.dumps(body), content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {capability}",
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["accepted"])
            self.assertNotIn("digest", response.content.decode())

        signals = list(ResponseSessionMatchSignal.objects.order_by("pk"))
        self.assertEqual(len(signals), 2)
        self.assertNotEqual(signals[0].device_key_digest, body["device_key"])
        self.assertNotEqual(signals[0].fingerprint_digest, body["fingerprint"])
        progress = self.client.get(
            self.course_url("progress"),
        )
        self.assertEqual(progress.status_code, 200)
        self.assertEqual([row["label"] for row in progress.json()["students"]], ["S1"])
        serialized = progress.content.decode()
        self.assertNotIn("legacy-device-key", serialized)
        self.assertNotIn("visitor-fingerprint", serialized)
        self.assertNotIn("device_key_digest", serialized)

    def test_student_survey_metadata_exposes_only_matching_enabled_state(self):
        occurrence = self.make_occurrence(compiled_protocol={
            "version": 1,
            "title": "Reflection",
            "intro": "Share your feedback.",
            "scales": {},
            "sections": [{
                "id": "section_a",
                "title": "Reflection",
                "items": [{
                    "id": "question_a",
                    "prompt": "What helped?",
                    "wording": "exact",
                    "response": {"kind": "text"},
                    "reflection_goal": "Explain",
                }],
            }],
        })

        disabled = self.client.get(ROOT + f"surveys/{occurrence.public_id}/")
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["anonymous_matching_enabled"])

        self.patch_settings(True)
        enabled = self.client.get(ROOT + f"surveys/{occurrence.public_id}/")
        self.assertTrue(enabled.json()["anonymous_matching_enabled"])
        self.assertNotIn("device_key_digest", enabled.content.decode())
        self.assertNotIn("fingerprint_digest", enabled.content.decode())
