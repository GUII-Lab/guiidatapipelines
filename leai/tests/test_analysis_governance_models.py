from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from leai.models import (
    AuthoringConversation, Course, CourseMembership, Institution,
    InstitutionMembership, InstructorAccount, QuestionSet, QuestionSetDraft,
    QuestionSetDraftVersion, QuestionSetRevision, ResponseMessage, ResponseSession,
    SurveyOccurrence,
)
from leai.models.analysis import (
    AnalysisChatMessage, AnalysisChatSession, AnalysisCitation,
    AnalysisScopeOccurrence, AnalysisSnapshot,
)
from leai.models.governance import AuditEvent, ImportRecordMap, ImportRecordOutcome, ImportRun


class AnalysisGovernanceTests(TestCase):
    def setUp(self):
        institution = Institution.objects.create(slug="analysis-test", name="Analysis Test")
        self.course = Course.objects.create(
            institution=institution, course_code="test-course", name="Test Course"
        )
        user = get_user_model().objects.create_user(username="analyst", email="analyst@example.edu")
        self.actor = InstructorAccount.objects.create(
            user=user, email="analyst@example.edu", display_name="Analyst"
        )
        membership = InstitutionMembership.objects.create(
            account=self.actor, institution=institution, role="instructor"
        )
        CourseMembership.objects.create(
            course=self.course, institution_membership=membership, role="instructor"
        )

    def make_snapshot(self, **overrides):
        values = dict(
            course=self.course, scope_key="all", algorithm_version="v1",
            response_cutoff_version=1, model_policy_version="v1",
            prompt_policy_version="v1", source_count=0, status="completed",
        )
        values.update(overrides)
        return AnalysisSnapshot.objects.create(**values)

    def make_chat_message(self):
        chat = AnalysisChatSession.objects.create(
            course=self.course, actor_account=self.actor, origin_surface="analyzer"
        )
        return AnalysisChatMessage.objects.create(
            analysis_chat_session=chat, sequence=1, role="user",
            input_method="typed", content="What changed?"
        )

    def make_import_run(self, **overrides):
        values = dict(
            actor_account=self.actor, run_kind="dry_run", source_environment="production",
            target_environment="qa", idempotency_key_hash="a" * 64,
            manifest_digest="b" * 64, status="completed", source_release="v1",
            target_release="v2", migration_set="0001-0007", contract_version="v1",
        )
        values.update(overrides)
        return ImportRun.objects.create(**values)

    def make_response_message(self, course=None):
        course = course or self.course
        question_set = QuestionSet.objects.create(
            course=course, owner=self.actor, title="Reflection",
            audience="individual", collection_style="guided",
        )
        draft = QuestionSetDraft.objects.create(
            question_set=question_set, updated_by=self.actor, canonical_body={}
        )
        draft_version = QuestionSetDraftVersion.objects.create(
            draft=draft, version_number=1, content_hash="a" * 64,
            canonical_body={}, change_kind="manual", created_by=self.actor,
        )
        revision = QuestionSetRevision.objects.create(
            question_set=question_set, revision_number=1,
            source_draft_version=draft_version, content_hash="a" * 64,
            compiled_protocol={}, compiler_version="v1", engine_version="v1",
            created_by=self.actor,
        )
        occurrence = SurveyOccurrence.objects.create(
            revision=revision, course=course, created_by=self.actor, label="Week 1"
        )
        session = ResponseSession.objects.create(
            occurrence=occurrence, source="student", status="active",
            capability_nonce="nonce", capability_digest=f"{occurrence.pk:064x}",
            capability_key_version=1,
        )
        return ResponseMessage.objects.create(
            response_session=session, sequence=1, role="student",
            input_method="typed", content="A reflection", attribution={},
        )

    def test_snapshot_generation_inputs_are_unique(self):
        first = self.make_snapshot()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_snapshot()
        self.assertEqual(AnalysisSnapshot.objects.get(pk=first.pk).source_count, 0)

    def test_citation_requires_exactly_one_owner_and_source(self):
        message = self.make_chat_message()
        snapshot = self.make_snapshot()
        for values in (
            dict(analysis_chat_message=message, analysis_snapshot=snapshot, response_session_id=1),
            dict(analysis_chat_message=message, response_session_id=1, response_message_id=1),
            dict(analysis_chat_message=message),
        ):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                AnalysisCitation.objects.create(claim_key="claim-1", **values)

    def test_citation_cannot_point_across_courses_and_is_append_only(self):
        chat_message = self.make_chat_message()
        response_message = self.make_response_message()
        citation = AnalysisCitation.objects.create(
            analysis_chat_message=chat_message, response_message=response_message,
            claim_key="claim-1",
        )
        other_course = Course.objects.create(
            institution=self.course.institution, course_code="citation-other", name="Other"
        )
        other_message = self.make_response_message(course=other_course)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnalysisCitation.objects.create(
                analysis_chat_message=chat_message, response_message=other_message,
                claim_key="claim-2",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnalysisCitation.objects.filter(pk=citation.pk).update(claim_key="changed")
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnalysisCitation.objects.filter(pk=citation.pk).delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ResponseMessage.objects.filter(pk=response_message.pk).update(
                response_session=other_message.response_session
            )

    def test_scope_occurrence_requires_same_course(self):
        chat_message = self.make_chat_message()
        response_message = self.make_response_message()
        AnalysisScopeOccurrence.objects.create(
            analysis_chat_session=chat_message.analysis_chat_session,
            survey_occurrence=response_message.response_session.occurrence,
        )
        other_course = Course.objects.create(
            institution=self.course.institution, course_code="scope-other", name="Other"
        )
        other_message = self.make_response_message(course=other_course)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnalysisScopeOccurrence.objects.create(
                analysis_chat_session=chat_message.analysis_chat_session,
                survey_occurrence=other_message.response_session.occurrence,
            )

    def test_chat_owner_and_origin_cannot_change(self):
        chat = self.make_chat_message().analysis_chat_session
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnalysisChatSession.objects.filter(pk=chat.pk).update(origin_surface="feedback_chat")

    def test_analysis_chat_user_input_method_is_required_and_non_user_is_null(self):
        message = self.make_chat_message()
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnalysisChatMessage.objects.create(
                analysis_chat_session=message.analysis_chat_session, sequence=2,
                role="user", content="No input method"
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnalysisChatMessage.objects.create(
                analysis_chat_session=message.analysis_chat_session, sequence=2,
                role="assistant", input_method="voice", content="Response"
            )

    def test_append_only_rows_reject_bulk_update_and_delete(self):
        snapshot = self.make_snapshot()
        audit = AuditEvent.objects.create(
            actor_account=self.actor, course=self.course, actor_kind="instructor",
            action="analysis.view", outcome="allowed", target_type="course",
            target_id=str(self.course.pk), request_id="request-1", bounded_metadata={},
        )
        import_run = self.make_import_run()
        record_map = ImportRecordMap.objects.create(
            first_import_run=import_run, source_environment="production",
            target_environment="qa", source_model="Course", source_key="42",
            source_row_digest="d" * 64, target_model="Course", target_key="7",
        )
        for model, pk, field, value in (
            (AnalysisSnapshot, snapshot.pk, "source_count", 1),
            (AuditEvent, audit.pk, "outcome", "denied"),
            (ImportRecordMap, record_map.pk, "target_key", "8"),
        ):
            with self.subTest(model=model.__name__, action="update"):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.filter(pk=pk).update(**{field: value})
            with self.subTest(model=model.__name__, action="delete"):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.filter(pk=pk).delete()

    def test_authoring_source_snapshot_requires_same_course_and_actor_access(self):
        snapshot = self.make_snapshot()
        question_set = QuestionSet.objects.create(
            course=self.course, owner=self.actor, title="Reflection",
            audience="individual", collection_style="guided",
        )
        conversation = AuthoringConversation.objects.create(
            question_set=question_set, origin_surface="instructor_insights",
            created_by=self.actor, source_analysis_snapshot=snapshot,
        )
        self.assertEqual(conversation.source_analysis_snapshot_id, snapshot.pk)
        other_course = Course.objects.create(
            institution=self.course.institution, course_code="other", name="Other"
        )
        other_snapshot = self.make_snapshot(course=other_course)
        conversation.source_analysis_snapshot = other_snapshot
        with self.assertRaises(ValidationError):
            conversation.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSet.objects.filter(pk=question_set.pk).update(course=other_course)
        unapproved_user = get_user_model().objects.create_user(
            username="unapproved", email="unapproved@example.edu"
        )
        unapproved = InstructorAccount.objects.create(
            user=unapproved_user, email="unapproved@example.edu", display_name="Unapproved"
        )
        unapproved_question_set = QuestionSet.objects.create(
            course=self.course, owner=self.actor, title="Another reflection",
            audience="individual", collection_style="guided",
        )
        with self.assertRaises(ValidationError):
            AuthoringConversation.objects.create(
                question_set=unapproved_question_set, origin_surface="instructor_insights",
                created_by=unapproved, source_analysis_snapshot=snapshot,
            )

    def test_import_map_is_unique_across_runs(self):
        first = self.make_import_run()
        second = self.make_import_run(run_kind="execute", idempotency_key_hash="c" * 64)
        values = dict(
            source_environment="production", target_environment="qa", source_model="Course",
            source_key="42", source_row_digest="d" * 64, target_model="Course", target_key="7",
        )
        ImportRecordMap.objects.create(first_import_run=first, **values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImportRecordMap.objects.create(first_import_run=second, **values)

    def test_import_outcome_is_unique_per_run_source_row(self):
        run = self.make_import_run()
        values = dict(
            import_run=run, source_model="Course", source_key="42",
            disposition="quarantined", reason_code="unresolved_owner",
        )
        ImportRecordOutcome.objects.create(**values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImportRecordOutcome.objects.create(**values)

    def test_import_outcome_rejects_unknown_disposition_or_reason(self):
        run = self.make_import_run()
        for overrides in (
            {"disposition": "unknown", "reason_code": "none"},
            {"disposition": "quarantined", "reason_code": "free text"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(IntegrityError), transaction.atomic():
                ImportRecordOutcome.objects.create(
                    import_run=run, source_model="Course", source_key="42", **overrides
                )

    def test_quarantined_outcome_requires_specific_reason(self):
        run = self.make_import_run()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImportRecordOutcome.objects.create(
                import_run=run, source_model="LEAIChatSession", source_key="42",
                disposition="quarantined", reason_code="none",
            )

    def test_import_map_and_outcome_cannot_claim_different_source(self):
        run = self.make_import_run()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImportRecordMap.objects.create(
                first_import_run=run, source_environment="staging",
                target_environment="qa", source_model="Course", source_key="42",
                source_row_digest="d" * 64, target_model="Course", target_key="7",
            )
        record_map = ImportRecordMap.objects.create(
            first_import_run=run, source_environment="production",
            target_environment="qa", source_model="Course", source_key="42",
            source_row_digest="d" * 64, target_model="Course", target_key="7",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImportRecordOutcome.objects.create(
                import_run=run, import_record_map=record_map,
                source_model="Course", source_key="43", disposition="reused",
                reason_code="none",
            )

    def test_audit_metadata_must_be_bounded_object(self):
        values = dict(
            actor_account=self.actor, course=self.course, actor_kind="instructor",
            action="analysis.view", outcome="allowed", target_type="course",
            target_id=str(self.course.pk), request_id="request-1",
        )
        for metadata in ([], {"detail": "x" * 5000}):
            with self.subTest(metadata_type=type(metadata).__name__), self.assertRaises(IntegrityError), transaction.atomic():
                AuditEvent.objects.create(bounded_metadata=metadata, **values)

    def test_public_ids_cannot_be_updated(self):
        audit = AuditEvent.objects.create(
            actor_account=self.actor, course=self.course, actor_kind="instructor",
            action="analysis.view", outcome="allowed", target_type="course",
            target_id=str(self.course.pk), request_id="request-1", bounded_metadata={},
        )
        run = self.make_import_run()
        for model, pk, column in (
            (AuditEvent, audit.pk, "event_id"),
            (ImportRun, run.pk, "public_id"),
        ):
            with self.subTest(model=model.__name__), self.assertRaises(IntegrityError), transaction.atomic():
                model.objects.filter(pk=pk).update(**{column: "00000000-0000-4000-8000-000000000001"})
