from django.test import TestCase

from leai.models import PdfImportBatch, ResponseMessage, ResponseSession, SurveyOccurrence
from leai.tests.test_response_models import ResponseFixturesMixin
from leai.services.analysis_corpus import (
    OccurrenceScopeError,
    build_team_summaries,
    eligible_response_records,
    summarize_response_records,
)


class AnalysisCorpusTests(ResponseFixturesMixin, TestCase):
    def add_student_message(self, session, sequence, content, role="student"):
        return ResponseMessage.objects.create(
            response_session=session,
            sequence=sequence,
            role=role,
            input_method="typed" if role == "student" else None,
            content=content,
        )

    def test_population_counts_response_sessions_once_and_student_messages_as_turns(self):
        occurrence = self.make_occurrence()
        student = self.make_student_session(occurrence=occurrence)
        self.add_student_message(student, 1, "First answer has four words")
        self.add_student_message(student, 2, "Second student answer")
        self.add_student_message(student, 3, "Assistant text is excluded", role="assistant")

        assistant_only = self.make_student_session(occurrence=occurrence)
        self.add_student_message(assistant_only, 1, "Opening only", role="assistant")

        pdf_batch = self.make_pdf_batch(occurrence=occurrence, status="completed")
        pdf = self.make_pdf_session(occurrence=occurrence, batch=pdf_batch)
        self.add_student_message(pdf, 1, "Q: Prompt\n\nA: Imported answer", role="assistant")

        records = eligible_response_records(self.course, [occurrence.public_id])
        summary = summarize_response_records(records)

        self.assertEqual(summary["response_count"], 2)
        self.assertEqual(summary["student_turn_count"], 2)
        self.assertEqual(summary["pdf_response_count"], 1)
        self.assertEqual(summary["average_words"], 5.0)
        self.assertEqual({row["id"] for row in records}, {str(student.public_id), str(pdf.public_id)})
        pdf_record = next(row for row in records if row["id"] == str(pdf.public_id))
        self.assertEqual(pdf_record["pdf_answers"], [{
            "answer_id": str(pdf.messages.get(sequence=1).pk),
            "question": "Prompt",
            "value": "Imported answer",
        }])

    def test_population_excludes_assistant_only_sessions_and_filters_to_exact_course_occurrences(self):
        selected = self.make_occurrence()
        foreign = self.make_occurrence(course=self.other_course)
        assistant_only = self.make_student_session(occurrence=selected)
        self.add_student_message(assistant_only, 1, "AI opening", role="assistant")
        other_response = self.make_student_session(occurrence=foreign)
        self.add_student_message(other_response, 1, "Foreign course secret")

        self.assertEqual(eligible_response_records(self.course, [selected.public_id]), [])
        with self.assertRaises(OccurrenceScopeError):
            eligible_response_records(self.course, [foreign.public_id])

    def test_turn_distribution_is_available_only_for_one_schema_family(self):
        first = self.make_occurrence()
        second = SurveyOccurrence.objects.create(
            revision=first.revision,
            course=self.course,
            created_by=self.account,
            label="Week 2",
            provenance="native",
            management_mode="managed",
            settings_version=1,
        )
        third = self.make_occurrence()
        for occurrence, turn_count in ((first, 1), (second, 2), (third, 3)):
            session = self.make_student_session(occurrence=occurrence)
            for sequence in range(1, turn_count + 1):
                self.add_student_message(session, sequence, "Student turn")

        same_family = eligible_response_records(self.course, [first.public_id, second.public_id])
        mixed_family = eligible_response_records(self.course, [first.public_id, third.public_id])

        self.assertEqual(summarize_response_records(same_family)["turn_distribution"], [
            {"student_turn_count": 1, "response_count": 1},
            {"student_turn_count": 2, "response_count": 1},
        ])
        self.assertEqual(summarize_response_records(mixed_family)["turn_distribution"], {
            "state": "unavailable",
            "reason": "mixed_schema_families",
        })

    def test_participation_is_unavailable_without_authoritative_denominator(self):
        occurrence = self.make_occurrence()
        session = self.make_student_session(occurrence=occurrence)
        self.add_student_message(session, 1, "One response")

        summary = summarize_response_records(eligible_response_records(self.course))

        self.assertEqual(summary["participation"], {
            "state": "unavailable",
            "reason": "eligible_denominator_missing",
        })


    def test_team_records_keep_snapshot_identity_and_unlinked_responses_separate(self):
        first_occurrence = self.make_occurrence(audience="team")
        second_occurrence = self.make_occurrence(audience="team")
        first_item = self.make_team_item(occurrence=first_occurrence)
        second_item = self.make_team_item(occurrence=second_occurrence)
        first = self.make_student_session(
            occurrence=first_occurrence, team_snapshot_item=first_item,
        )
        second = self.make_student_session(
            occurrence=second_occurrence, team_snapshot_item=second_item,
        )
        unlinked = self.make_student_session(occurrence=first_occurrence)
        self.add_student_message(first, 1, "First team response")
        self.add_student_message(second, 1, "Second team response")
        self.add_student_message(unlinked, 1, "Unlinked response")

        summaries = build_team_summaries(eligible_response_records(self.course))

        self.assertEqual(len(summaries["teams"]), 2)
        self.assertNotEqual(
            summaries["teams"][0]["team_snapshot_id"],
            summaries["teams"][1]["team_snapshot_id"],
        )
        self.assertEqual([row["response_count"] for row in summaries["teams"]], [1, 1])
        self.assertEqual(summaries["unlinked"], [
            {"occurrence_id": str(first_occurrence.public_id), "response_count": 1},
        ])

    def test_question_health_uses_exact_compiled_item_ids_only(self):
        protocol = {
            "version": 1,
            "title": "Reflection",
            "intro": "Start",
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
        }
        occurrence = self.make_occurrence(compiled_protocol=protocol)
        session = self.make_student_session(occurrence=occurrence)
        ResponseMessage.objects.create(
            response_session=session,
            sequence=1,
            role="student",
            input_method="typed",
            content="Clear instructions.",
            attribution={"item_id": "question_a", "phase": "answer"},
        )

        summary = summarize_response_records(eligible_response_records(self.course))

        self.assertEqual(summary["question_health"], {
            "state": "available",
            "sections": [{
                "section_id": "section_a",
                "title": "Reflection",
                "response_count": 1,
                "questions": [{
                    "question_id": "question_a",
                    "prompt": "What helped?",
                    "response_count": 1,
                }],
            }],
        })
