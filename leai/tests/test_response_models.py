import queue
import threading
import time
import uuid

from django.contrib.auth import get_user_model
from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from leai.models import (
    Course,
    Institution,
    InstructorAccount,
    PdfImportBatch,
    PdfImportJob,
    QuestionSet,
    QuestionSetDraft,
    QuestionSetDraftVersion,
    QuestionSetRevision,
    ResponseMessage,
    ResponseSession,
    SurveyOccurrence,
    TeamConfiguration,
    TeamDefinition,
    TeamSnapshot,
    TeamSnapshotItem,
)


TEAM_OCCURRENCE_FK = "leai_response_session_team_occurrence_fk"
PDF_OCCURRENCE_FK = "leai_response_session_pdf_occurrence_fk"
SNAPSHOT_ITEM_OCCURRENCE_FK = "leai_team_snapshot_item_occurrence_fk"
SNAPSHOT_OCCURRENCE_COURSE_FK = "leai_team_snapshot_occurrence_course_fk"
SNAPSHOT_CONFIGURATION_COURSE_FK = "leai_team_snapshot_configuration_course_fk"


class ResponseFixturesMixin:
    def setUp(self):
        self._fixture_counter = 0
        self.institution = Institution.objects.create(
            slug="response-test-institution",
            name="Response Test Institution",
        )
        self.course = self.make_course("primary")
        self.other_course = self.make_course("other")
        self.account = self.make_account("primary")

    def next_suffix(self):
        self._fixture_counter += 1
        return str(self._fixture_counter)

    def make_account(self, suffix):
        email = f"response-{suffix}@example.edu"
        return InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username=f"response-{suffix}",
                email=email,
            ),
            email=email,
            display_name=f"Response {suffix}",
        )

    def make_course(self, suffix):
        return Course.objects.create(
            institution=self.institution,
            course_code=f"response-{suffix}",
            name=f"Response {suffix}",
        )

    def make_occurrence(self, *, course=None, audience="individual", compiled_protocol=None):
        course = course or self.course
        suffix = self.next_suffix()
        question_set = QuestionSet.objects.create(
            course=course,
            owner=self.account,
            title=f"Response questions {suffix}",
            audience=audience,
            collection_style="guided",
        )
        draft = QuestionSetDraft.objects.create(
            question_set=question_set,
            current_version=1,
            canonical_body={},
            updated_by=self.account,
        )
        draft_version = QuestionSetDraftVersion.objects.create(
            draft=draft,
            version_number=1,
            content_hash=f"{int(suffix):064x}",
            canonical_body={},
            created_by=self.account,
        )
        revision = QuestionSetRevision.objects.create(
            question_set=question_set,
            revision_number=1,
            source_draft_version=draft_version,
            content_hash=f"{int(suffix):064x}",
            compiled_protocol=compiled_protocol if compiled_protocol is not None else {},
            compiler_version="1.0.0",
            engine_version="1.0.0",
            created_by=self.account,
        )
        return SurveyOccurrence.objects.create(
            revision=revision,
            course=course,
            created_by=self.account,
            label=f"Response occurrence {suffix}",
            provenance="native",
            management_mode="managed",
            settings_version=1,
        )

    def make_team_configuration(self, *, course=None):
        suffix = self.next_suffix()
        return TeamConfiguration.objects.create(
            course=course or self.course,
            name=f"Teams {suffix}",
            settings_version=1,
            created_by=self.account,
        )

    def make_team_item(self, *, occurrence=None, configuration=None):
        occurrence = occurrence or self.make_occurrence(audience="team")
        configuration = configuration or self.make_team_configuration(
            course=occurrence.course,
        )
        suffix = self.next_suffix()
        with transaction.atomic():
            snapshot = TeamSnapshot.objects.create(
                occurrence=occurrence,
                source_configuration=configuration,
                course=occurrence.course,
                frozen_at=None,
            )
            item = TeamSnapshotItem.objects.create(
                snapshot=snapshot,
                occurrence=occurrence,
                item_number=1,
                stable_key=f"team-{suffix}",
                label=f"Team {suffix}",
            )
            snapshot.frozen_at = timezone.now()
            snapshot.save(update_fields=["frozen_at"])
        return item

    def make_pdf_batch(self, *, occurrence=None, **overrides):
        occurrence = occurrence or self.make_occurrence()
        suffix = self.next_suffix()
        values = {
            "occurrence": occurrence,
            "committed_by": self.account,
            "idempotency_key_hash": f"{1000 + int(suffix):064x}",
            "manifest_digest": f"{2000 + int(suffix):064x}",
            "status": "prepared",
            "manifest": {},
        }
        values.update(overrides)
        return PdfImportBatch.objects.create(**values)

    def make_student_session(self, *, occurrence=None, **overrides):
        occurrence = occurrence or self.make_occurrence()
        suffix = self.next_suffix()
        values = {
            "occurrence": occurrence,
            "capability_nonce": f"one-session-nonce-{suffix}",
            "capability_digest": f"{3000 + int(suffix):064x}",
            "capability_key_version": 1,
            "source": "student",
            "status": "active",
            "research_consent": False,
        }
        values.update(overrides)
        return ResponseSession.objects.create(**values)

    def make_pdf_session(self, *, occurrence=None, batch=None, **overrides):
        occurrence = occurrence or self.make_occurrence()
        batch = batch or self.make_pdf_batch(occurrence=occurrence)
        values = {
            "occurrence": occurrence,
            "pdf_import_batch": batch,
            "source": "pdf",
            "status": "active",
            "research_consent": False,
        }
        values.update(overrides)
        return ResponseSession.objects.create(**values)

    @staticmethod
    def force_constraints(*constraint_names):
        with connection.cursor() as cursor:
            cursor.execute(f"SET CONSTRAINTS {', '.join(constraint_names)} IMMEDIATE")

    def assert_integrity_code(self, operation, expected_code="23514"):
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                operation()
        self.assertEqual(caught.exception.__cause__.pgcode, expected_code)


class ResponseModelTests(ResponseFixturesMixin, TestCase):
    def test_pdf_session_requires_batch_and_forbids_capability(self):
        occurrence = self.make_occurrence()

        with self.assertRaises(IntegrityError), transaction.atomic():
            ResponseSession.objects.create(
                occurrence=occurrence,
                source="pdf",
                capability_nonce="raw-not-allowed",
                capability_digest="a" * 64,
                capability_key_version=1,
                status="active",
                research_consent=False,
            )

        batch = self.make_pdf_batch(occurrence=occurrence)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ResponseSession.objects.create(
                occurrence=occurrence,
                pdf_import_batch=batch,
                capability_nonce="still-not-allowed",
                source="pdf",
                status="active",
                research_consent=False,
            )

    def test_student_session_requires_capability_and_forbids_pdf_batch(self):
        occurrence = self.make_occurrence()
        required_capability_sets = [
            {},
            {
                "capability_nonce": "nonce",
                "capability_digest": "a" * 64,
            },
            {
                "capability_nonce": "nonce",
                "capability_key_version": 1,
            },
            {
                "capability_digest": "a" * 64,
                "capability_key_version": 1,
            },
        ]
        for capabilities in required_capability_sets:
            with self.subTest(capabilities=capabilities), self.assertRaises(
                IntegrityError,
            ), transaction.atomic():
                ResponseSession.objects.create(
                    occurrence=occurrence,
                    source="student",
                    status="active",
                    research_consent=False,
                    **capabilities,
                )

        batch = self.make_pdf_batch(occurrence=occurrence)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_student_session(
                occurrence=occurrence,
                pdf_import_batch=batch,
            )

    def test_session_vocabulary_capability_and_completion_checks_are_database_enforced(self):
        invalid_sessions = [
            {"source": "named-student"},
            {"status": "invalid"},
            {"capability_nonce": ""},
            {"capability_digest": "A" * 64},
            {"capability_key_version": 0},
            {"next_message_sequence": 0},
            {"turn_version": 0},
            {"completed_at": timezone.now()},
            {"completion_snapshot": {"complete": True}},
        ]
        for values in invalid_sessions:
            with self.subTest(values=values), self.assertRaises(
                IntegrityError,
            ), transaction.atomic():
                self.make_student_session(**values)

        session = self.make_student_session()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_student_session(capability_digest=session.capability_digest)

        completed_at = timezone.now()
        completed = self.make_student_session(
            status="completed",
            completed_at=completed_at,
            completion_snapshot={"completed_at": completed_at.isoformat()},
        )
        self.assertEqual(completed.status, "completed")

    def test_message_sequence_is_unique_per_session(self):
        session = self.make_student_session()
        ResponseMessage.objects.create(
            response_session=session,
            sequence=1,
            role="student",
            input_method="typed",
            content="one",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ResponseMessage.objects.create(
                response_session=session,
                sequence=1,
                role="student",
                input_method="typed",
                content="two",
            )

    def test_message_role_input_shape_and_bounded_attribution_are_database_enforced(self):
        session = self.make_student_session()
        invalid_messages = [
            {"sequence": 0, "role": "student", "input_method": "typed"},
            {"sequence": 1, "role": "named-student", "input_method": None},
            {"sequence": 1, "role": "assistant", "input_method": "voice"},
            {"sequence": 1, "role": "student", "input_method": "fingerprint"},
            {"sequence": 1, "role": "student", "input_method": None, "attribution": []},
            {
                "sequence": 1,
                "role": "student",
                "input_method": None,
                "attribution": {"text": "x" * 17000},
            },
        ]
        for values in invalid_messages:
            values.setdefault("attribution", {})
            with self.subTest(values=values), self.assertRaises(
                IntegrityError,
            ), transaction.atomic():
                ResponseMessage.objects.create(
                    response_session=session,
                    content="message",
                    **values,
                )

        message = ResponseMessage.objects.create(
            response_session=session,
            sequence=1,
            role="student",
            input_method=None,
            content="student input method may be absent",
            attribution={"question_id": "q1"},
        )
        self.assertIsNone(message.input_method)

    def test_team_configuration_snapshot_and_items_enforce_occurrence_ownership(self):
        team_occurrence = self.make_occurrence(audience="team")
        other_occurrence = self.make_occurrence(
            course=self.other_course,
            audience="team",
        )
        configuration = self.make_team_configuration(course=self.course)
        TeamDefinition.objects.create(
            configuration=configuration,
            stable_key="team-a",
            label="Team A",
            sort_order=1,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            TeamDefinition.objects.create(
                configuration=configuration,
                stable_key="team-a",
                label="Duplicate Team A",
                sort_order=2,
            )

        with transaction.atomic():
            snapshot = TeamSnapshot.objects.create(
                occurrence=team_occurrence,
                source_configuration=configuration,
                course=team_occurrence.course,
                frozen_at=None,
            )
            TeamSnapshotItem.objects.create(
                snapshot=snapshot,
                occurrence=team_occurrence,
                item_number=1,
                stable_key="team-a",
                label="Team A",
            )

            with self.assertRaises(IntegrityError), transaction.atomic():
                TeamSnapshotItem.objects.create(
                    snapshot=snapshot,
                    occurrence=team_occurrence,
                    item_number=1,
                    stable_key="team-b",
                    label="Team B",
                )

            with self.assertRaises(IntegrityError), transaction.atomic():
                TeamSnapshotItem.objects.create(
                    snapshot=snapshot,
                    occurrence=other_occurrence,
                    item_number=2,
                    stable_key="team-b",
                    label="Team B",
                )
                self.force_constraints(SNAPSHOT_ITEM_OCCURRENCE_FK)

            snapshot.frozen_at = timezone.now()
            snapshot.save(update_fields=["frozen_at"])

        wrong_course_configuration = self.make_team_configuration(
            course=self.other_course,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            TeamSnapshot.objects.create(
                occurrence=team_occurrence,
                source_configuration=wrong_course_configuration,
                course=team_occurrence.course,
                frozen_at=timezone.now(),
            )
            self.force_constraints(SNAPSHOT_CONFIGURATION_COURSE_FK)

        with self.assertRaises(IntegrityError), transaction.atomic():
            TeamConfiguration.objects.filter(pk=configuration.pk).update(
                course=self.other_course,
            )

    def test_empty_team_configuration_and_occurrence_without_snapshot_are_valid(self):
        configuration = self.make_team_configuration()
        occurrence = self.make_occurrence(audience="team")

        self.assertFalse(configuration.definitions.exists())
        self.assertFalse(TeamSnapshot.objects.filter(occurrence=occurrence).exists())

    def test_team_snapshot_items_are_built_atomically_before_freeze(self):
        occurrence = self.make_occurrence(audience="team")
        configuration = self.make_team_configuration(course=occurrence.course)

        with transaction.atomic():
            snapshot = TeamSnapshot.objects.create(
                occurrence=occurrence,
                source_configuration=configuration,
                course=occurrence.course,
                frozen_at=None,
            )
            item = TeamSnapshotItem.objects.create(
                snapshot=snapshot,
                occurrence=occurrence,
                item_number=1,
                stable_key="atomic-team",
                label="Atomic Team",
            )
            snapshot.frozen_at = timezone.now()
            snapshot.save(update_fields=["frozen_at"])

        self.assertEqual(snapshot.items.get(), item)
        self.assertIsNotNone(snapshot.frozen_at)

    def test_direct_orm_rejects_late_item_insert_into_frozen_snapshot(self):
        item = self.make_team_item()

        self.assert_integrity_code(
            lambda: TeamSnapshotItem.objects.create(
                snapshot=item.snapshot,
                occurrence=item.occurrence,
                item_number=2,
                stable_key="late-team",
                label="Late Team",
            ),
        )

    def test_raw_sql_rejects_late_item_insert_into_frozen_snapshot(self):
        item = self.make_team_item()

        def insert_late_item():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO leai_teamsnapshotitem
                        (snapshot_id, occurrence_id, item_number, stable_key, label)
                    VALUES (%s, %s, 2, 'raw-late-team', 'Raw Late Team')
                    """,
                    [item.snapshot_id, item.occurrence_id],
                )

        self.assert_integrity_code(insert_late_item)

    def test_parent_delete_reinsert_cannot_change_a_snapshot_course(self):
        item = self.make_team_item()
        snapshot = item.snapshot
        other_occurrence = self.make_occurrence(
            course=self.other_course,
            audience="team",
        )

        def replace_configuration():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM leai_teamconfiguration WHERE id = %s",
                    [snapshot.source_configuration_id],
                )
                cursor.execute(
                    """
                    INSERT INTO leai_teamconfiguration
                        (id, course_id, name, settings_version, created_by_id,
                         created_at, updated_at)
                    VALUES (%s, %s, 'Replacement Teams', 1, %s,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    [
                        snapshot.source_configuration_id,
                        self.other_course.pk,
                        self.account.pk,
                    ],
                )
            self.force_constraints("ALL")

        def replace_occurrence():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM leai_surveyoccurrence WHERE id = %s",
                    [snapshot.occurrence_id],
                )
                cursor.execute(
                    """
                    INSERT INTO leai_surveyoccurrence
                        (id, public_id, revision_id, course_id, created_by_id,
                         label, provenance, management_mode, opens_at, closes_at,
                         manually_closed_at, settings_version,
                         completion_certificate_enabled,
                         completed_response_download_enabled, created_at, updated_at)
                    VALUES (%s, gen_random_uuid(), %s, %s, %s,
                            'Replacement Occurrence', 'native', 'managed',
                            NULL, NULL, NULL, 1, FALSE, FALSE,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    [
                        snapshot.occurrence_id,
                        other_occurrence.revision_id,
                        self.other_course.pk,
                        self.account.pk,
                    ],
                )
            self.force_constraints("ALL")

        for replacement in (replace_configuration, replace_occurrence):
            with self.subTest(replacement=replacement.__name__):
                self.assert_integrity_code(replacement, expected_code="23503")

    def test_response_team_and_pdf_assignments_must_share_occurrence(self):
        first_occurrence = self.make_occurrence(audience="team")
        second_occurrence = self.make_occurrence(audience="team")
        team_item = self.make_team_item(occurrence=first_occurrence)
        batch = self.make_pdf_batch(occurrence=first_occurrence)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_student_session(
                occurrence=second_occurrence,
                team_snapshot_item=team_item,
            )
            self.force_constraints(TEAM_OCCURRENCE_FK)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_pdf_session(
                occurrence=second_occurrence,
                batch=batch,
            )
            self.force_constraints(PDF_OCCURRENCE_FK)

    def test_occurrence_ownership_and_team_snapshots_are_immutable_via_model_save(self):
        first_occurrence = self.make_occurrence(audience="team")
        second_occurrence = self.make_occurrence(audience="team")
        item = self.make_team_item(occurrence=first_occurrence)
        snapshot = item.snapshot
        session = self.make_student_session(occurrence=first_occurrence)
        batch = self.make_pdf_batch(occurrence=first_occurrence)

        def save_to_other_occurrence(instance):
            instance.occurrence = second_occurrence
            instance.save(update_fields=["occurrence"])

        for instance in (session, batch, snapshot, item):
            with self.subTest(model=type(instance).__name__):
                self.assert_integrity_code(
                    lambda instance=instance: save_to_other_occurrence(instance),
                )

        snapshot.refresh_from_db()
        snapshot.frozen_at = snapshot.frozen_at + timezone.timedelta(seconds=1)
        self.assert_integrity_code(
            lambda: snapshot.save(update_fields=["frozen_at"]),
        )

        item.refresh_from_db()
        item.label = "Mutated Team"
        self.assert_integrity_code(lambda: item.save(update_fields=["label"]))

    def test_bulk_updates_and_deletes_cannot_migrate_frozen_occurrence_graphs(self):
        first_occurrence = self.make_occurrence(audience="team")
        second_occurrence = self.make_occurrence(audience="team")
        team_item = self.make_team_item(occurrence=first_occurrence)
        team_session = self.make_student_session(occurrence=first_occurrence)
        batch = self.make_pdf_batch(occurrence=first_occurrence)
        empty_snapshot = TeamSnapshot.objects.create(
            occurrence=self.make_occurrence(audience="team"),
            source_configuration=self.make_team_configuration(course=self.course),
            course=self.course,
            frozen_at=timezone.now(),
        )

        invalid_updates = [
            lambda: ResponseSession.objects.filter(pk=team_session.pk).update(
                occurrence=second_occurrence,
            ),
            lambda: PdfImportBatch.objects.filter(pk=batch.pk).update(
                occurrence=second_occurrence,
            ),
            lambda: TeamSnapshot.objects.filter(pk=team_item.snapshot_id).update(
                occurrence=second_occurrence,
            ),
            lambda: TeamSnapshotItem.objects.filter(pk=team_item.pk).update(
                occurrence=second_occurrence,
            ),
            lambda: TeamSnapshot.objects.filter(pk=team_item.snapshot_id).update(
                frozen_at=timezone.now(),
            ),
            lambda: TeamSnapshotItem.objects.filter(pk=team_item.pk).update(
                label="Mutated Team",
            ),
        ]
        for update in invalid_updates:
            with self.subTest(update=update):
                self.assert_integrity_code(update)

        for delete in (
            lambda: TeamSnapshotItem.objects.filter(pk=team_item.pk).delete(),
            lambda: TeamSnapshot.objects.filter(pk=empty_snapshot.pk).delete(),
        ):
            with self.subTest(delete=delete):
                self.assert_integrity_code(delete)

    def test_response_public_ids_are_immutable_with_check_violation_sqlstate(self):
        session = self.make_student_session()
        batch = self.make_pdf_batch(occurrence=session.occurrence)

        for model, pk in (
            (ResponseSession, session.pk),
            (PdfImportBatch, batch.pk),
        ):
            with self.subTest(model=model.__name__):
                self.assert_integrity_code(
                    lambda model=model, pk=pk: model.objects.filter(pk=pk).update(
                        public_id=uuid.uuid4(),
                    ),
                )

    def test_pdf_idempotency_key_is_unique_per_occurrence(self):
        first_occurrence = self.make_occurrence()
        second_occurrence = self.make_occurrence()
        shared_key = "d" * 64
        self.make_pdf_batch(
            occurrence=first_occurrence,
            idempotency_key_hash=shared_key,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_pdf_batch(
                occurrence=first_occurrence,
                idempotency_key_hash=shared_key,
            )

        allowed = self.make_pdf_batch(
            occurrence=second_occurrence,
            idempotency_key_hash=shared_key,
        )
        self.assertEqual(allowed.occurrence, second_occurrence)

    def test_raw_sql_cannot_mutate_occurrence_ownership_or_frozen_team_rows(self):
        first_occurrence = self.make_occurrence(audience="team")
        second_occurrence = self.make_occurrence(audience="team")
        session = self.make_student_session(occurrence=first_occurrence)
        batch = self.make_pdf_batch(occurrence=first_occurrence)
        item = self.make_team_item(occurrence=first_occurrence)

        writes = [
            (
                "UPDATE leai_responsesession SET occurrence_id = %s WHERE id = %s",
                [second_occurrence.pk, session.pk],
            ),
            (
                "UPDATE leai_pdfimportbatch SET occurrence_id = %s WHERE id = %s",
                [second_occurrence.pk, batch.pk],
            ),
            (
                "UPDATE leai_teamsnapshot SET occurrence_id = %s WHERE id = %s",
                [second_occurrence.pk, item.snapshot_id],
            ),
            (
                "UPDATE leai_teamsnapshotitem SET label = %s WHERE id = %s",
                ["Mutated Team", item.pk],
            ),
            (
                "DELETE FROM leai_teamsnapshotitem WHERE id = %s",
                [item.pk],
            ),
        ]
        for sql, params in writes:
            with self.subTest(sql=sql):
                self.assert_integrity_code(
                    lambda sql=sql, params=params: connection.cursor().execute(
                        sql,
                        params,
                    ),
                )

    def test_raw_sql_composite_foreign_keys_reject_cross_occurrence_rows(self):
        first_occurrence = self.make_occurrence(audience="team")
        second_occurrence = self.make_occurrence(audience="team")
        team_item = self.make_team_item(occurrence=first_occurrence)
        batch = self.make_pdf_batch(occurrence=first_occurrence)
        construction_occurrence = self.make_occurrence(audience="team")
        construction_configuration = self.make_team_configuration(
            course=construction_occurrence.course,
        )

        def insert_mismatched_snapshot_item():
            snapshot = TeamSnapshot.objects.create(
                occurrence=construction_occurrence,
                source_configuration=construction_configuration,
                course=construction_occurrence.course,
                frozen_at=None,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO leai_teamsnapshotitem
                        (snapshot_id, occurrence_id, item_number, stable_key, label)
                    VALUES (%s, %s, 1, 'raw-cross-occurrence',
                            'Raw Cross Occurrence')
                    """,
                    [snapshot.pk, second_occurrence.pk],
                )
            self.force_constraints(SNAPSHOT_ITEM_OCCURRENCE_FK)

        self.assert_integrity_code(
            insert_mismatched_snapshot_item,
            expected_code="23503",
        )

        invalid_inserts = [
            (
                """
                INSERT INTO leai_responsesession
                    (occurrence_id, team_snapshot_item_id, capability_nonce,
                     capability_digest, capability_key_version, source, status,
                     research_consent, next_message_sequence, turn_version,
                     created_at, updated_at)
                VALUES (%s, %s, 'raw-team-nonce', %s, 1, 'student', 'active',
                        FALSE, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                [second_occurrence.pk, team_item.pk, "8" * 64],
                TEAM_OCCURRENCE_FK,
            ),
            (
                """
                INSERT INTO leai_responsesession
                    (occurrence_id, pdf_import_batch_id, source, status,
                     research_consent, next_message_sequence, turn_version,
                     created_at, updated_at)
                VALUES (%s, %s, 'pdf', 'active', FALSE, 1, 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                [second_occurrence.pk, batch.pk],
                PDF_OCCURRENCE_FK,
            ),
        ]

        for sql, params, constraint_name in invalid_inserts:
            with self.subTest(constraint=constraint_name):
                def insert_and_check():
                    with connection.cursor() as cursor:
                        cursor.execute(sql, params)
                    self.force_constraints(constraint_name)

                self.assert_integrity_code(insert_and_check, expected_code="23503")

    def test_pdf_batch_job_and_team_definition_constraints_are_database_enforced(self):
        batch = self.make_pdf_batch()
        PdfImportJob.objects.create(
            batch=batch,
            job_number=1,
            status="pending",
        )

        invalid_jobs = [
            {"job_number": 0, "status": "pending"},
            {"job_number": 2, "status": "invalid"},
            {"job_number": 2, "status": "pending", "result": []},
            {
                "job_number": 2,
                "status": "pending",
                "result": {"text": "x" * 17000},
            },
        ]
        for values in invalid_jobs:
            with self.subTest(values=values), self.assertRaises(
                IntegrityError,
            ), transaction.atomic():
                PdfImportJob.objects.create(batch=batch, **values)

        with self.assertRaises(IntegrityError), transaction.atomic():
            PdfImportJob.objects.create(
                batch=batch,
                job_number=1,
                status="pending",
            )

        invalid_batches = [
            {"status": "invalid"},
            {"idempotency_key_hash": "A" * 64},
            {"manifest_digest": "a" * 63},
        ]
        for values in invalid_batches:
            with self.subTest(values=values), self.assertRaises(
                IntegrityError,
            ), transaction.atomic():
                self.make_pdf_batch(**values)

    def test_database_supplies_public_ids_for_direct_sql_inserts(self):
        occurrence = self.make_occurrence()
        batch = self.make_pdf_batch(occurrence=occurrence)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO leai_responsesession
                    (occurrence_id, pdf_import_batch_id, source, status,
                     research_consent, next_message_sequence, turn_version,
                     created_at, updated_at)
                VALUES (%s, %s, 'pdf', 'active', FALSE, 1, 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING public_id
                """,
                [occurrence.pk, batch.pk],
            )
            session_public_id = cursor.fetchone()[0]

            cursor.execute(
                """
                INSERT INTO leai_pdfimportbatch
                    (occurrence_id, committed_by_id, idempotency_key_hash,
                     manifest_digest, status, manifest, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'prepared', '{}'::jsonb,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING public_id
                """,
                [occurrence.pk, self.account.pk, "e" * 64, "f" * 64],
            )
            batch_public_id = cursor.fetchone()[0]

        self.assertIsNotNone(session_public_id)
        self.assertIsNotNone(batch_public_id)

    def test_response_schema_has_no_student_identity_or_tracking_fields(self):
        forbidden_fragments = {
            "account",
            "device",
            "fingerprint",
            "ip_address",
            "raw_url",
            "roster",
            "student_id",
            "user",
        }
        response_field_names = {
            field.name
            for model in (ResponseSession, ResponseMessage)
            for field in model._meta.get_fields()
            if getattr(field, "concrete", False)
        }

        for fragment in forbidden_fragments:
            self.assertFalse(
                any(fragment in field_name for field_name in response_field_names),
                (fragment, response_field_names),
            )
        self.assertNotIn("source_analysis_snapshot", response_field_names)


class ResponseOccurrenceConcurrencyTests(ResponseFixturesMixin, TransactionTestCase):
    thread_timeout_seconds = 5

    @staticmethod
    def error_details(error):
        cause = error.__cause__
        return cause.pgcode, cause.diag.message_primary

    def blocking_pids(self, database_pid):
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_blocking_pids(%s)", [database_pid])
            return cursor.fetchone()[0]

    def test_team_snapshot_construction_cannot_commit_unfrozen(self):
        occurrence = self.make_occurrence(audience="team")
        configuration = self.make_team_configuration(course=occurrence.course)

        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                TeamSnapshot.objects.create(
                    occurrence=occurrence,
                    source_configuration=configuration,
                    course=occurrence.course,
                    frozen_at=None,
                )

        self.assertEqual(caught.exception.__cause__.pgcode, "23514")
        self.assertFalse(TeamSnapshot.objects.filter(occurrence=occurrence).exists())

    def run_immutable_parent_child_race(
        self,
        *,
        parent_write,
        child_write,
        parent_first,
    ):
        parent_attempted = threading.Event()
        child_inserted = threading.Event()
        parent_finished = threading.Event()
        child_finished = threading.Event()
        release_parent = threading.Event()
        release_child = threading.Event()
        outcomes = queue.Queue()

        def parent():
            try:
                close_old_connections()
                if not parent_first and not child_inserted.wait(
                    self.thread_timeout_seconds,
                ):
                    raise RuntimeError("child insert did not begin")
                with transaction.atomic():
                    try:
                        with transaction.atomic():
                            parent_write()
                    except IntegrityError as error:
                        outcomes.put(("parent", "integrity", *self.error_details(error)))
                    else:
                        outcomes.put(("parent", "changed"))
                    parent_attempted.set()
                    if parent_first and not release_parent.wait(
                        self.thread_timeout_seconds,
                    ):
                        raise RuntimeError("parent transaction was not released")
            except DatabaseError as error:
                outcomes.put(("parent", "database_error", *self.error_details(error)))
            except Exception as error:
                outcomes.put(("parent", "error", repr(error)))
            finally:
                parent_finished.set()
                close_old_connections()

        def child():
            try:
                close_old_connections()
                if parent_first and not parent_attempted.wait(
                    self.thread_timeout_seconds,
                ):
                    raise RuntimeError("parent mutation did not begin")
                with transaction.atomic():
                    child_write()
                    child_inserted.set()
                    if not parent_first and not release_child.wait(
                        self.thread_timeout_seconds,
                    ):
                        raise RuntimeError("child transaction was not released")
                outcomes.put(("child", "committed"))
            except IntegrityError as error:
                outcomes.put(("child", "integrity", *self.error_details(error)))
            except DatabaseError as error:
                outcomes.put(("child", "database_error", *self.error_details(error)))
            except Exception as error:
                outcomes.put(("child", "error", repr(error)))
            finally:
                child_finished.set()
                close_old_connections()

        parent_thread = threading.Thread(target=parent)
        child_thread = threading.Thread(target=child)
        parent_thread.start()
        child_thread.start()

        if parent_first:
            completed_without_blocking = child_finished.wait(
                self.thread_timeout_seconds,
            )
            release_parent.set()
        else:
            completed_without_blocking = parent_finished.wait(
                self.thread_timeout_seconds,
            )
            release_child.set()

        parent_thread.join(self.thread_timeout_seconds)
        child_thread.join(self.thread_timeout_seconds)
        self.assertTrue(completed_without_blocking, "race operation blocked")
        self.assertFalse(parent_thread.is_alive(), "parent race thread did not finish")
        self.assertFalse(child_thread.is_alive(), "child race thread did not finish")

        results = {}
        while not outcomes.empty():
            result = outcomes.get_nowait()
            results[result[0]] = result[1:]
        self.assertEqual(results.get("parent", ())[:2], ("integrity", "23514"), results)
        self.assertEqual(results.get("child"), ("committed",), results)

    def make_item_session_race(self):
        original_occurrence = self.make_occurrence(audience="team")
        other_occurrence = self.make_occurrence(audience="team")
        item = self.make_team_item(occurrence=original_occurrence)
        return (
            lambda: TeamSnapshotItem.objects.filter(pk=item.pk).update(
                occurrence=other_occurrence,
            ),
            lambda: self.make_student_session(
                occurrence=original_occurrence,
                team_snapshot_item=item,
            ),
        )

    def make_batch_session_race(self):
        original_occurrence = self.make_occurrence()
        other_occurrence = self.make_occurrence()
        batch = self.make_pdf_batch(occurrence=original_occurrence)
        return (
            lambda: PdfImportBatch.objects.filter(pk=batch.pk).update(
                occurrence=other_occurrence,
            ),
            lambda: self.make_pdf_session(
                occurrence=original_occurrence,
                batch=batch,
            ),
        )

    def make_session_message_race(self):
        original_occurrence = self.make_occurrence()
        other_occurrence = self.make_occurrence()
        session = self.make_student_session(occurrence=original_occurrence)
        return (
            lambda: ResponseSession.objects.filter(pk=session.pk).update(
                occurrence=other_occurrence,
            ),
            lambda: ResponseMessage.objects.create(
                response_session=session,
                sequence=1,
                role="student",
                input_method="typed",
                content="Concurrent message",
            ),
        )

    def test_occurrence_parent_child_races_reject_parent_first_and_child_first_moves(self):
        case_factories = (
            self.make_item_session_race,
            self.make_batch_session_race,
            self.make_session_message_race,
        )
        for case_factory in case_factories:
            for parent_first in (True, False):
                with self.subTest(
                    relation=case_factory.__name__,
                    order="parent-first" if parent_first else "child-first",
                ):
                    parent_write, child_write = case_factory()
                    self.run_immutable_parent_child_race(
                        parent_write=parent_write,
                        child_write=child_write,
                        parent_first=parent_first,
                    )

    def test_concurrent_late_item_inserts_cannot_extend_frozen_snapshot(self):
        item = self.make_team_item()
        start = threading.Barrier(3)
        outcomes = queue.Queue()

        def insert_late_item(item_number):
            try:
                close_old_connections()
                start.wait(self.thread_timeout_seconds)
                with transaction.atomic():
                    TeamSnapshotItem.objects.create(
                        snapshot_id=item.snapshot_id,
                        occurrence_id=item.occurrence_id,
                        item_number=item_number,
                        stable_key=f"concurrent-late-{item_number}",
                        label=f"Concurrent Late {item_number}",
                    )
                outcomes.put(("committed", item_number))
            except IntegrityError as error:
                outcomes.put(("integrity", item_number, *self.error_details(error)))
            except Exception as error:
                outcomes.put(("error", item_number, repr(error)))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=insert_late_item, args=(item_number,))
            for item_number in (2, 3)
        ]
        for thread in threads:
            thread.start()
        start.wait(self.thread_timeout_seconds)
        for thread in threads:
            thread.join(self.thread_timeout_seconds)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = sorted(outcomes.get_nowait() for _ in threads)
        self.assertEqual(
            [result[0:2] + result[2:3] for result in results],
            [
                ("integrity", 2, "23514"),
                ("integrity", 3, "23514"),
            ],
            results,
        )
        self.assertEqual(
            TeamSnapshotItem.objects.filter(snapshot_id=item.snapshot_id).count(),
            1,
        )

    def test_concurrent_parent_replacement_cannot_commit_stale_course_snapshot(self):
        occurrence = self.make_occurrence(audience="team")
        configuration = self.make_team_configuration(course=occurrence.course)
        replacement_ready = threading.Event()
        insert_started = threading.Event()
        insert_finished = threading.Event()
        release_replacement = threading.Event()
        release_snapshot_commit = threading.Event()
        outcomes = queue.Queue()

        def replace_configuration():
            try:
                close_old_connections()
                with transaction.atomic():
                    with connections["default"].cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM leai_teamconfiguration WHERE id = %s",
                            [configuration.pk],
                        )
                        cursor.execute(
                            """
                            INSERT INTO leai_teamconfiguration
                                (id, course_id, name, settings_version,
                                 created_by_id, created_at, updated_at)
                            VALUES (%s, %s, 'Concurrent Replacement', 1, %s,
                                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            """,
                            [
                                configuration.pk,
                                self.other_course.pk,
                                self.account.pk,
                            ],
                        )
                    replacement_ready.set()
                    if not release_replacement.wait(self.thread_timeout_seconds):
                        raise RuntimeError("replacement was not released")
                outcomes.put(("replacement", "committed"))
            except IntegrityError as error:
                outcomes.put(
                    ("replacement", "integrity", *self.error_details(error)),
                )
            except Exception as error:
                outcomes.put(("replacement", "error", repr(error)))
            finally:
                close_old_connections()

        def create_snapshot():
            try:
                close_old_connections()
                if not replacement_ready.wait(self.thread_timeout_seconds):
                    raise RuntimeError("replacement did not begin")
                with transaction.atomic():
                    insert_started.set()
                    TeamSnapshot.objects.create(
                        occurrence_id=occurrence.pk,
                        source_configuration_id=configuration.pk,
                        course_id=occurrence.course_id,
                        frozen_at=timezone.now(),
                    )
                    insert_finished.set()
                    if not release_snapshot_commit.wait(
                        self.thread_timeout_seconds,
                    ):
                        raise RuntimeError("snapshot commit was not released")
                outcomes.put(("snapshot", "committed"))
            except IntegrityError as error:
                outcomes.put(("snapshot", "integrity", *self.error_details(error)))
            except DatabaseError as error:
                outcomes.put(
                    ("snapshot", "database_error", *self.error_details(error)),
                )
            except Exception as error:
                outcomes.put(("snapshot", "error", repr(error)))
            finally:
                close_old_connections()

        replacement_thread = threading.Thread(target=replace_configuration)
        snapshot_thread = threading.Thread(target=create_snapshot)
        replacement_thread.start()
        snapshot_thread.start()
        self.assertTrue(insert_started.wait(self.thread_timeout_seconds))
        inserted_before_replacement_commit = insert_finished.wait(1)

        release_replacement.set()
        replacement_thread.join(self.thread_timeout_seconds)
        release_snapshot_commit.set()
        snapshot_thread.join(self.thread_timeout_seconds)

        self.assertTrue(inserted_before_replacement_commit)
        self.assertFalse(replacement_thread.is_alive())
        self.assertFalse(snapshot_thread.is_alive())
        results = {}
        while not outcomes.empty():
            result = outcomes.get_nowait()
            results[result[0]] = result[1:]
        self.assertEqual(results.get("replacement"), ("committed",), results)
        self.assertEqual(results.get("snapshot", ())[:2], ("integrity", "23503"), results)
        self.assertFalse(TeamSnapshot.objects.filter(occurrence=occurrence).exists())

    def test_team_snapshot_insert_does_not_block_on_inverse_parent_lock_order(self):
        occurrence = self.make_occurrence(audience="team")
        configuration = self.make_team_configuration(course=occurrence.course)
        locks_held = threading.Event()
        insert_started = threading.Event()
        insert_completed = threading.Event()
        release_locks = threading.Event()
        release_insert = threading.Event()
        pids = queue.Queue()
        outcomes = queue.Queue()

        def lock_parents():
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                pids.put(("locker", database.connection.get_backend_pid()))
                with transaction.atomic():
                    SurveyOccurrence.objects.select_for_update().get(pk=occurrence.pk)
                    TeamConfiguration.objects.select_for_update().get(
                        pk=configuration.pk,
                    )
                    locks_held.set()
                    if not release_locks.wait(self.thread_timeout_seconds):
                        raise RuntimeError("parent locks were not released")
                outcomes.put(("locker", "committed"))
            except Exception as error:
                outcomes.put(("locker", "error", repr(error)))
            finally:
                close_old_connections()

        def insert_snapshot():
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                pids.put(("inserter", database.connection.get_backend_pid()))
                if not locks_held.wait(self.thread_timeout_seconds):
                    raise RuntimeError("parent locks were not acquired")
                with transaction.atomic():
                    insert_started.set()
                    TeamSnapshot.objects.create(
                        occurrence=occurrence,
                        source_configuration=configuration,
                        course=occurrence.course,
                        frozen_at=timezone.now(),
                    )
                    insert_completed.set()
                    if not release_insert.wait(self.thread_timeout_seconds):
                        raise RuntimeError("snapshot insert was not released")
                outcomes.put(("inserter", "committed"))
            except Exception as error:
                outcomes.put(("inserter", "error", repr(error)))
            finally:
                close_old_connections()

        locker_thread = threading.Thread(target=lock_parents)
        inserter_thread = threading.Thread(target=insert_snapshot)
        locker_thread.start()
        inserter_thread.start()
        self.assertTrue(insert_started.wait(self.thread_timeout_seconds))
        pid_values = dict(pids.get(timeout=5) for _ in range(2))

        blockers = []
        for _ in range(100):
            if insert_completed.is_set():
                break
            blockers = self.blocking_pids(pid_values["inserter"])
            if blockers:
                break
            time.sleep(0.01)
        completed_while_locked = insert_completed.is_set()

        release_locks.set()
        locker_thread.join(self.thread_timeout_seconds)
        insert_completed.wait(self.thread_timeout_seconds)
        release_insert.set()
        inserter_thread.join(self.thread_timeout_seconds)

        self.assertTrue(completed_while_locked, {"blocking_pids": blockers})
        self.assertNotIn(pid_values["locker"], blockers)
        self.assertFalse(locker_thread.is_alive(), "locker thread did not finish")
        self.assertFalse(inserter_thread.is_alive(), "inserter thread did not finish")
        results = {}
        while not outcomes.empty():
            result = outcomes.get_nowait()
            results[result[0]] = result[1:]
        self.assertEqual(results.get("locker"), ("committed",), results)
        self.assertEqual(results.get("inserter"), ("committed",), results)

    def test_composite_foreign_keys_are_explicitly_deferred(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT conname, condeferrable, condeferred
                FROM pg_constraint
                WHERE conname = ANY(%s)
                """,
                [[
                    TEAM_OCCURRENCE_FK,
                    PDF_OCCURRENCE_FK,
                    SNAPSHOT_ITEM_OCCURRENCE_FK,
                    SNAPSHOT_OCCURRENCE_COURSE_FK,
                    SNAPSHOT_CONFIGURATION_COURSE_FK,
                ]],
            )
            constraints = {name: (deferrable, deferred) for name, deferrable, deferred in cursor}

        self.assertEqual(
            constraints,
            {
                TEAM_OCCURRENCE_FK: (True, True),
                PDF_OCCURRENCE_FK: (True, True),
                SNAPSHOT_ITEM_OCCURRENCE_FK: (True, True),
                SNAPSHOT_OCCURRENCE_COURSE_FK: (True, True),
                SNAPSHOT_CONFIGURATION_COURSE_FK: (True, True),
            },
        )
