import queue
import threading
import time

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

    def make_occurrence(self, *, course=None, audience="individual"):
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
            compiled_protocol={},
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
        snapshot = TeamSnapshot.objects.create(
            occurrence=occurrence,
            source_configuration=configuration,
            frozen_at=timezone.now(),
        )
        suffix = self.next_suffix()
        return TeamSnapshotItem.objects.create(
            snapshot=snapshot,
            occurrence=occurrence,
            item_number=1,
            stable_key=f"team-{suffix}",
            label=f"Team {suffix}",
        )

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

        snapshot = TeamSnapshot.objects.create(
            occurrence=team_occurrence,
            source_configuration=configuration,
            frozen_at=timezone.now(),
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

        wrong_course_configuration = self.make_team_configuration(
            course=self.other_course,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            TeamSnapshot.objects.create(
                occurrence=team_occurrence,
                source_configuration=wrong_course_configuration,
                frozen_at=timezone.now(),
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            TeamConfiguration.objects.filter(pk=configuration.pk).update(
                course=self.other_course,
            )

    def test_empty_team_configuration_and_occurrence_without_snapshot_are_valid(self):
        configuration = self.make_team_configuration()
        occurrence = self.make_occurrence(audience="team")

        self.assertFalse(configuration.definitions.exists())
        self.assertFalse(TeamSnapshot.objects.filter(occurrence=occurrence).exists())

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

    def test_bulk_updates_cannot_break_existing_occurrence_relations(self):
        first_occurrence = self.make_occurrence(audience="team")
        second_occurrence = self.make_occurrence(audience="team")
        team_item = self.make_team_item(occurrence=first_occurrence)
        team_session = self.make_student_session(
            occurrence=first_occurrence,
            team_snapshot_item=team_item,
        )
        batch = self.make_pdf_batch(occurrence=first_occurrence)
        pdf_session = self.make_pdf_session(
            occurrence=first_occurrence,
            batch=batch,
        )

        invalid_updates = [
            lambda: ResponseSession.objects.filter(pk=team_session.pk).update(
                occurrence=second_occurrence,
            ),
            lambda: TeamSnapshotItem.objects.filter(pk=team_item.pk).update(
                occurrence=second_occurrence,
            ),
            lambda: ResponseSession.objects.filter(pk=pdf_session.pk).update(
                occurrence=second_occurrence,
            ),
            lambda: PdfImportBatch.objects.filter(pk=batch.pk).update(
                occurrence=second_occurrence,
            ),
        ]
        for update in invalid_updates:
            with self.subTest(update=update), self.assertRaises(
                IntegrityError,
            ), transaction.atomic():
                update()
                self.force_constraints(TEAM_OCCURRENCE_FK, PDF_OCCURRENCE_FK)

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

    def wait_for_blocker(self, blocked_pid, blocker_pid, finished, label):
        for _ in range(100):
            if blocker_pid in self.blocking_pids(blocked_pid):
                return True
            if finished.is_set():
                return False
            time.sleep(0.01)
        self.fail(f"{label} did not block on the conflicting occurrence write")

    def run_parent_first_race(
        self,
        *,
        parent_write,
        child_write,
        constraint_name,
    ):
        parent_ready = threading.Event()
        child_started = threading.Event()
        child_finished = threading.Event()
        release_parent = threading.Event()
        pids = queue.Queue()
        outcomes = queue.Queue()

        def parent():
            database_pid = None
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                database_pid = database.connection.get_backend_pid()
                with transaction.atomic():
                    parent_write()
                    pids.put(("parent", database_pid))
                    parent_ready.set()
                    if not release_parent.wait(self.thread_timeout_seconds):
                        raise RuntimeError("parent transaction was not released")
                outcomes.put(("parent", "committed", database_pid))
            except Exception as error:
                outcomes.put(("parent", "error", database_pid, repr(error)))
            finally:
                close_old_connections()

        def child():
            database_pid = None
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                database_pid = database.connection.get_backend_pid()
                pids.put(("child", database_pid))
                if not parent_ready.wait(self.thread_timeout_seconds):
                    raise RuntimeError("parent mutation did not begin")
                with transaction.atomic():
                    child_started.set()
                    child_write()
                    with database.cursor() as cursor:
                        cursor.execute(f"SET CONSTRAINTS {constraint_name} IMMEDIATE")
                outcomes.put(("child", "committed", database_pid))
            except IntegrityError as error:
                outcomes.put(
                    ("child", "integrity", database_pid, *self.error_details(error)),
                )
            except DatabaseError as error:
                outcomes.put(
                    ("child", "database_error", database_pid, *self.error_details(error)),
                )
            except Exception as error:
                outcomes.put(("child", "error", database_pid, repr(error)))
            finally:
                child_finished.set()
                close_old_connections()

        parent_thread = threading.Thread(target=parent)
        child_thread = threading.Thread(target=child)
        parent_thread.start()
        self.assertTrue(parent_ready.wait(self.thread_timeout_seconds))
        child_thread.start()
        self.assertTrue(child_started.wait(self.thread_timeout_seconds))

        pid_values = dict(pids.get(timeout=5) for _ in range(2))
        try:
            self.wait_for_blocker(
                pid_values["child"],
                pid_values["parent"],
                child_finished,
                "child",
            )
        finally:
            release_parent.set()
            parent_thread.join(self.thread_timeout_seconds)
            child_thread.join(self.thread_timeout_seconds)

        self.assertFalse(parent_thread.is_alive(), "parent race thread did not finish")
        self.assertFalse(child_thread.is_alive(), "child race thread did not finish")
        results = {
            result[0]: result[1:]
            for result in (
                outcomes.get(timeout=self.thread_timeout_seconds),
                outcomes.get(timeout=self.thread_timeout_seconds),
            )
        }
        self.assertEqual(results["parent"][0], "committed", results)
        self.assertEqual(results["child"][0], "integrity", results)
        self.assertEqual(results["child"][2], "23503", results)

    def run_child_first_race(
        self,
        *,
        parent_write,
        child_write,
        constraint_name,
    ):
        child_ready = threading.Event()
        parent_started = threading.Event()
        parent_finished = threading.Event()
        release_child = threading.Event()
        pids = queue.Queue()
        outcomes = queue.Queue()

        def child():
            database_pid = None
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                database_pid = database.connection.get_backend_pid()
                with transaction.atomic():
                    child_write()
                    with database.cursor() as cursor:
                        cursor.execute(f"SET CONSTRAINTS {constraint_name} IMMEDIATE")
                    pids.put(("child", database_pid))
                    child_ready.set()
                    if not release_child.wait(self.thread_timeout_seconds):
                        raise RuntimeError("child transaction was not released")
                outcomes.put(("child", "committed", database_pid))
            except Exception as error:
                outcomes.put(("child", "error", database_pid, repr(error)))
            finally:
                close_old_connections()

        def parent():
            database_pid = None
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                database_pid = database.connection.get_backend_pid()
                pids.put(("parent", database_pid))
                if not child_ready.wait(self.thread_timeout_seconds):
                    raise RuntimeError("child relation did not become valid")
                with transaction.atomic():
                    parent_started.set()
                    parent_write()
                outcomes.put(("parent", "committed", database_pid))
            except IntegrityError as error:
                outcomes.put(
                    ("parent", "integrity", database_pid, *self.error_details(error)),
                )
            except DatabaseError as error:
                outcomes.put(
                    ("parent", "database_error", database_pid, *self.error_details(error)),
                )
            except Exception as error:
                outcomes.put(("parent", "error", database_pid, repr(error)))
            finally:
                parent_finished.set()
                close_old_connections()

        child_thread = threading.Thread(target=child)
        parent_thread = threading.Thread(target=parent)
        child_thread.start()
        self.assertTrue(child_ready.wait(self.thread_timeout_seconds))
        parent_thread.start()
        self.assertTrue(parent_started.wait(self.thread_timeout_seconds))

        pid_values = dict(pids.get(timeout=5) for _ in range(2))
        try:
            self.wait_for_blocker(
                pid_values["parent"],
                pid_values["child"],
                parent_finished,
                "parent",
            )
        finally:
            release_child.set()
            child_thread.join(self.thread_timeout_seconds)
            parent_thread.join(self.thread_timeout_seconds)

        self.assertFalse(child_thread.is_alive(), "child race thread did not finish")
        self.assertFalse(parent_thread.is_alive(), "parent race thread did not finish")
        results = {
            result[0]: result[1:]
            for result in (
                outcomes.get(timeout=self.thread_timeout_seconds),
                outcomes.get(timeout=self.thread_timeout_seconds),
            )
        }
        self.assertEqual(results["child"][0], "committed", results)
        self.assertEqual(results["parent"][0], "integrity", results)
        self.assertEqual(results["parent"][2], "23503", results)

    def assert_parent_child_orders_are_safe(
        self,
        *,
        make_parent,
        parent_write,
        child_write,
        constraint_name,
    ):
        parent = make_parent()
        self.run_parent_first_race(
            parent_write=lambda: parent_write(parent),
            child_write=lambda: child_write(parent),
            constraint_name=constraint_name,
        )

        parent = make_parent()
        self.run_child_first_race(
            parent_write=lambda: parent_write(parent),
            child_write=lambda: child_write(parent),
            constraint_name=constraint_name,
        )

    def test_team_item_occurrence_parent_child_orders_are_safe(self):
        def make_case():
            original_occurrence = self.make_occurrence(audience="team")
            other_occurrence = self.make_occurrence(audience="team")
            item = self.make_team_item(occurrence=original_occurrence)

            def move_snapshot_and_item():
                TeamSnapshot.objects.filter(pk=item.snapshot_id).update(
                    occurrence=other_occurrence,
                )
                TeamSnapshotItem.objects.filter(pk=item.pk).update(
                    occurrence=other_occurrence,
                )

            def create_response():
                self.make_student_session(
                    occurrence=original_occurrence,
                    team_snapshot_item=item,
                )

            return move_snapshot_and_item, create_response

        parent_write, child_write = make_case()
        self.run_parent_first_race(
            parent_write=parent_write,
            child_write=child_write,
            constraint_name=TEAM_OCCURRENCE_FK,
        )

        parent_write, child_write = make_case()
        self.run_child_first_race(
            parent_write=parent_write,
            child_write=child_write,
            constraint_name=TEAM_OCCURRENCE_FK,
        )

    def test_team_snapshot_occurrence_parent_child_orders_are_safe(self):
        def make_case():
            original_occurrence = self.make_occurrence(audience="team")
            other_occurrence = self.make_occurrence(audience="team")
            configuration = self.make_team_configuration(
                course=original_occurrence.course,
            )
            snapshot = TeamSnapshot.objects.create(
                occurrence=original_occurrence,
                source_configuration=configuration,
                frozen_at=timezone.now(),
            )
            suffix = self.next_suffix()

            def move_snapshot():
                TeamSnapshot.objects.filter(pk=snapshot.pk).update(
                    occurrence=other_occurrence,
                )

            def create_item():
                TeamSnapshotItem.objects.create(
                    snapshot=snapshot,
                    occurrence=original_occurrence,
                    item_number=1,
                    stable_key=f"race-team-{suffix}",
                    label=f"Race Team {suffix}",
                )

            return move_snapshot, create_item

        parent_write, child_write = make_case()
        self.run_parent_first_race(
            parent_write=parent_write,
            child_write=child_write,
            constraint_name=SNAPSHOT_ITEM_OCCURRENCE_FK,
        )

        parent_write, child_write = make_case()
        self.run_child_first_race(
            parent_write=parent_write,
            child_write=child_write,
            constraint_name=SNAPSHOT_ITEM_OCCURRENCE_FK,
        )

    def test_pdf_batch_occurrence_parent_child_orders_are_safe(self):
        original_occurrence = self.make_occurrence()
        other_occurrence = self.make_occurrence()

        self.assert_parent_child_orders_are_safe(
            make_parent=lambda: self.make_pdf_batch(occurrence=original_occurrence),
            parent_write=lambda batch: PdfImportBatch.objects.filter(pk=batch.pk).update(
                occurrence=other_occurrence,
            ),
            child_write=lambda batch: self.make_pdf_session(
                occurrence=original_occurrence,
                batch=batch,
            ),
            constraint_name=PDF_OCCURRENCE_FK,
        )

    def test_occurrence_foreign_keys_are_explicitly_deferred(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT conname, condeferrable, condeferred
                FROM pg_constraint
                WHERE conname = ANY(%s)
                """,
                [[TEAM_OCCURRENCE_FK, PDF_OCCURRENCE_FK]],
            )
            constraints = {name: (deferrable, deferred) for name, deferrable, deferred in cursor}

        self.assertEqual(
            constraints,
            {
                TEAM_OCCURRENCE_FK: (True, True),
                PDF_OCCURRENCE_FK: (True, True),
            },
        )
