import threading
import time

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

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
            prompt_policy_version="v1", source_count=0,
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

    def test_uncited_response_message_rejects_orm_and_raw_sql_update_delete(self):
        table = ResponseMessage._meta.db_table
        writes = (
            lambda pk: ResponseMessage.objects.filter(pk=pk).update(content="changed"),
            lambda pk: ResponseMessage.objects.filter(pk=pk).delete(),
            lambda pk: self._raw_write(f"UPDATE {table} SET content = %s WHERE id = %s", ["changed", pk]),
            lambda pk: self._raw_write(f"DELETE FROM {table} WHERE id = %s", [pk]),
        )
        for write in writes:
            message = self.make_response_message()
            with self.subTest(write=write):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    write(message.pk)
                message.refresh_from_db()
                self.assertEqual(message.content, "A reflection")

    @staticmethod
    def _raw_write(sql, params):
        with connection.cursor() as cursor:
            cursor.execute(sql, params)

    def test_scope_occurrence_requires_same_course(self):
        chat = AnalysisChatSession.objects.create(
            course=self.course, actor_account=self.actor, origin_surface="analyzer"
        )
        response_message = self.make_response_message()
        AnalysisScopeOccurrence.objects.create(
            analysis_chat_session=chat,
            survey_occurrence=response_message.response_session.occurrence,
        )
        other_course = Course.objects.create(
            institution=self.course.institution, course_code="scope-other", name="Other"
        )
        other_message = self.make_response_message(course=other_course)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnalysisScopeOccurrence.objects.create(
                analysis_chat_session=chat,
                survey_occurrence=other_message.response_session.occurrence,
            )

    def test_scope_occurrences_freeze_after_first_analysis_message(self):
        chat = AnalysisChatSession.objects.create(
            course=self.course, actor_account=self.actor, origin_surface="analyzer"
        )
        first_occurrence = self.make_response_message().response_session.occurrence
        second_occurrence = self.make_response_message().response_session.occurrence
        scope = AnalysisScopeOccurrence.objects.create(
            analysis_chat_session=chat, survey_occurrence=first_occurrence
        )
        AnalysisChatMessage.objects.create(
            analysis_chat_session=chat, sequence=1, role="user",
            input_method="typed", content="What changed?",
        )
        for action, write in (
            ("update", lambda: AnalysisScopeOccurrence.objects.filter(pk=scope.pk).update(
                survey_occurrence=second_occurrence
            )),
            ("delete", lambda: AnalysisScopeOccurrence.objects.filter(pk=scope.pk).delete()),
            ("insert", lambda: AnalysisScopeOccurrence.objects.create(
                analysis_chat_session=chat, survey_occurrence=second_occurrence
            )),
        ):
            with self.subTest(action=action), self.assertRaises(IntegrityError), transaction.atomic():
                write()
        self.assertEqual(AnalysisScopeOccurrence.objects.filter(analysis_chat_session=chat).count(), 1)

    def test_scope_update_and_delete_are_allowed_before_first_message(self):
        chat = AnalysisChatSession.objects.create(
            course=self.course, actor_account=self.actor, origin_surface="analyzer"
        )
        first_occurrence = self.make_response_message().response_session.occurrence
        second_occurrence = self.make_response_message().response_session.occurrence
        scope = AnalysisScopeOccurrence.objects.create(
            analysis_chat_session=chat, survey_occurrence=first_occurrence
        )
        self.assertEqual(
            AnalysisScopeOccurrence.objects.filter(pk=scope.pk).update(
                survey_occurrence=second_occurrence
            ),
            1,
        )
        self.assertEqual(AnalysisScopeOccurrence.objects.filter(pk=scope.pk).delete()[0], 1)
        self.assertFalse(AnalysisScopeOccurrence.objects.filter(analysis_chat_session=chat).exists())

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
        import_run = self.make_import_run(run_kind="execute")
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

    def test_authoring_conversation_creation_identity_is_immutable(self):
        question_set = QuestionSet.objects.create(
            course=self.course, owner=self.actor, title="Reflection",
            audience="individual", collection_style="guided",
        )
        other_question_set = QuestionSet.objects.create(
            course=self.course, owner=self.actor, title="Other reflection",
            audience="individual", collection_style="guided",
        )
        other_user = get_user_model().objects.create_user(
            username="other-creator", email="other-creator@example.edu"
        )
        other_actor = InstructorAccount.objects.create(
            user=other_user, email="other-creator@example.edu", display_name="Other creator"
        )
        conversation = AuthoringConversation.objects.create(
            question_set=question_set, origin_surface="builder", created_by=self.actor
        )
        for field, value in (
            ("created_by", other_actor),
            ("question_set", other_question_set),
            ("source_analysis_snapshot", self.make_snapshot()),
            ("origin_surface", "survey_list"),
        ):
            with self.subTest(field=field), self.assertRaises(IntegrityError), transaction.atomic():
                AuthoringConversation.objects.filter(pk=conversation.pk).update(**{field: value})

    def test_insights_origin_requires_snapshot(self):
        question_set = QuestionSet.objects.create(
            course=self.course, owner=self.actor, title="Insight reflection",
            audience="individual", collection_style="guided",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuthoringConversation.objects.create(
                question_set=question_set, origin_surface="instructor_insights",
                created_by=self.actor,
            )

    def test_non_insights_origin_forbids_source_snapshot(self):
        question_set = QuestionSet.objects.create(
            course=self.course, owner=self.actor, title="Builder reflection",
            audience="individual", collection_style="guided",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuthoringConversation.objects.bulk_create([
                AuthoringConversation(
                    question_set=question_set, origin_surface="builder",
                    created_by=self.actor, source_analysis_snapshot=self.make_snapshot(),
                )
            ])

    def test_analysis_snapshot_has_no_job_status_column(self):
        self.assertNotIn("status", {field.name for field in AnalysisSnapshot._meta.fields})
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, AnalysisSnapshot._meta.db_table
            )
        self.assertNotIn("status", {column.name for column in columns})

    def test_import_map_is_unique_across_runs(self):
        first = self.make_import_run(run_kind="execute")
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
        run = self.make_import_run(run_kind="execute")
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

    def test_durable_import_map_requires_execute_run(self):
        dry_run = self.make_import_run()
        values = dict(
            source_environment="production", target_environment="qa",
            source_model="Course", source_key="42", source_row_digest="d" * 64,
            target_model="Course", target_key="7",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImportRecordMap.objects.create(first_import_run=dry_run, **values)
        execution = self.make_import_run(run_kind="execute", idempotency_key_hash="c" * 64)
        record_map = ImportRecordMap.objects.create(first_import_run=execution, **values)
        self.assertEqual(record_map.first_import_run_id, execution.pk)

    def test_execute_mapping_outcomes_require_matching_map_but_dry_run_does_not(self):
        dry_run = self.make_import_run()
        ImportRecordOutcome.objects.create(
            import_run=dry_run, source_model="Course", source_key="42",
            disposition="mapped", reason_code="none",
        )
        execution = self.make_import_run(run_kind="execute", idempotency_key_hash="c" * 64)
        for disposition in ("mapped", "reused"):
            with self.subTest(disposition=disposition), self.assertRaises(IntegrityError), transaction.atomic():
                ImportRecordOutcome.objects.create(
                    import_run=execution, source_model="Course", source_key="42",
                    disposition=disposition, reason_code="none",
                )

    def test_completed_import_run_outcomes_reject_update_and_delete(self):
        run = self.make_import_run()
        outcome = ImportRecordOutcome.objects.create(
            import_run=run, source_model="LEAIChatSession", source_key="42",
            disposition="quarantined", reason_code="unresolved_owner",
        )
        for write in (
            lambda: ImportRecordOutcome.objects.filter(pk=outcome.pk).update(reason_code="unresolved_scope"),
            lambda: ImportRecordOutcome.objects.filter(pk=outcome.pk).delete(),
        ):
            with self.subTest(write=write), self.assertRaises(IntegrityError), transaction.atomic():
                write()

    def test_reopened_import_run_cannot_mutate_existing_outcomes(self):
        run = self.make_import_run()
        outcome = ImportRecordOutcome.objects.create(
            import_run=run, source_model="LEAIChatSession", source_key="42",
            disposition="quarantined", reason_code="unresolved_owner",
        )
        ImportRun.objects.filter(pk=run.pk).update(status="running")
        for action, write in (
            ("update", lambda: ImportRecordOutcome.objects.filter(pk=outcome.pk).update(
                reason_code="unresolved_scope"
            )),
            ("delete", lambda: ImportRecordOutcome.objects.filter(pk=outcome.pk).delete()),
        ):
            with self.subTest(action=action), self.assertRaises(IntegrityError), transaction.atomic():
                write()

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


class AnalysisScopeConcurrencyTests(TransactionTestCase):
    def test_scope_insert_waits_for_uncommitted_first_message(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock behavior")
        institution = Institution.objects.create(slug="scope-race", name="Scope Race")
        course = Course.objects.create(
            institution=institution, course_code="scope-race", name="Scope Race"
        )
        user = get_user_model().objects.create_user(username="scope-race")
        actor = InstructorAccount.objects.create(
            user=user, email="scope-race@example.edu", display_name="Scope Race"
        )
        question_set = QuestionSet.objects.create(
            course=course, owner=actor, title="Reflection",
            audience="individual", collection_style="guided",
        )
        draft = QuestionSetDraft.objects.create(
            question_set=question_set, updated_by=actor, canonical_body={}
        )
        version = QuestionSetDraftVersion.objects.create(
            draft=draft, version_number=1, content_hash="a" * 64,
            canonical_body={}, change_kind="manual", created_by=actor,
        )
        revision = QuestionSetRevision.objects.create(
            question_set=question_set, revision_number=1,
            source_draft_version=version, content_hash="a" * 64,
            compiled_protocol={}, compiler_version="v1", engine_version="v1",
            created_by=actor,
        )
        occurrence = SurveyOccurrence.objects.create(
            revision=revision, course=course, created_by=actor, label="Week 1"
        )
        chat = AnalysisChatSession.objects.create(
            course=course, actor_account=actor, origin_surface="analyzer"
        )

        message_inserted = threading.Event()
        release_message = threading.Event()
        scope_started = threading.Event()
        scope_done = threading.Event()
        scope_backend_pid = {}
        scope_result = {}
        worker_errors = []

        def insert_message():
            try:
                with transaction.atomic():
                    AnalysisChatMessage.objects.create(
                        analysis_chat_session=chat, sequence=1, role="user",
                        input_method="typed", content="First message",
                    )
                    message_inserted.set()
                    if not release_message.wait(10):
                        raise TimeoutError("message transaction was not released")
            except Exception as exc:
                worker_errors.append(exc)
            finally:
                message_inserted.set()
                connections["default"].close()

        def insert_scope():
            try:
                if not message_inserted.wait(10):
                    raise TimeoutError("message was not inserted")
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    scope_backend_pid["value"] = cursor.fetchone()[0]
                scope_started.set()
                try:
                    with transaction.atomic():
                        AnalysisScopeOccurrence.objects.create(
                            analysis_chat_session=chat, survey_occurrence=occurrence
                        )
                except IntegrityError:
                    scope_result["value"] = "rejected"
                else:
                    scope_result["value"] = "inserted"
            except Exception as exc:
                worker_errors.append(exc)
            finally:
                scope_started.set()
                scope_done.set()
                connections["default"].close()

        message_thread = threading.Thread(target=insert_message)
        scope_thread = threading.Thread(target=insert_scope)
        message_thread.start()
        saw_lock_wait = False
        try:
            if message_inserted.wait(5):
                scope_thread.start()
                if scope_started.wait(5) and "value" in scope_backend_pid:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not scope_done.is_set():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                                [scope_backend_pid["value"]],
                            )
                            row = cursor.fetchone()
                        if row and row[0] == "Lock":
                            saw_lock_wait = True
                            break
                        time.sleep(0.02)
        finally:
            release_message.set()
            message_thread.join(10)
            if scope_thread.ident is not None:
                scope_thread.join(10)

        self.assertFalse(message_thread.is_alive())
        self.assertFalse(scope_thread.is_alive())
        self.assertEqual(worker_errors, [])
        self.assertTrue(saw_lock_wait, "scope insert did not wait on the chat parent lock")
        self.assertEqual(scope_result.get("value"), "rejected")
        self.assertFalse(AnalysisScopeOccurrence.objects.filter(analysis_chat_session=chat).exists())
