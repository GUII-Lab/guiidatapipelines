from django.contrib.auth import get_user_model
import queue
import threading

from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TestCase, TransactionTestCase

from leai.models import (
    Course,
    Institution,
    InstructorAccount,
    QuestionSet,
    QuestionSetDraft,
    QuestionSetDraftVersion,
    QuestionSetRevision,
)


def assert_trigger_rejection(test_case, write, message):
    with test_case.assertRaises(IntegrityError) as raised, transaction.atomic():
        write()

    cause = raised.exception.__cause__
    test_case.assertEqual(cause.pgcode, "23514")
    test_case.assertEqual(cause.diag.message_primary, message)


class AuthoringModelTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(
            slug="authoring-test-institution",
            name="Authoring Test Institution",
        )
        self.course = Course.objects.create(
            institution=self.institution,
            course_code="authoring-test-course",
            name="Authoring Test Course",
        )
        self.account = self.make_account("primary")

    def make_account(self, suffix):
        email = f"authoring-{suffix}@example.edu"
        return InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username=f"authoring-{suffix}",
                email=email,
            ),
            email=email,
            display_name=f"Authoring {suffix}",
        )

    def make_question_set(self, **overrides):
        values = {
            "course": self.course,
            "owner": self.account,
            "title": "Reflection questions",
            "audience": "individual",
            "collection_style": "guided",
        }
        values.update(overrides)
        return QuestionSet.objects.create(**values)

    def make_draft(self, **overrides):
        question_set = overrides.pop("question_set", self.make_question_set())
        values = {
            "question_set": question_set,
            "current_version": 1,
            "canonical_body": {},
            "updated_by": self.account,
        }
        values.update(overrides)
        return QuestionSetDraft.objects.create(**values)

    def make_draft_version(self, **overrides):
        question_set = overrides.pop("question_set", None)
        draft = overrides.pop(
            "draft",
            self.make_draft(question_set=question_set) if question_set else self.make_draft(),
        )
        values = {
            "draft": draft,
            "version_number": 1,
            "content_hash": "a" * 64,
            "canonical_body": {},
            "created_by": self.account,
        }
        values.update(overrides)
        return QuestionSetDraftVersion.objects.create(**values)

    def make_revision(self, **overrides):
        question_set = overrides.pop("question_set", self.make_question_set())
        source_draft_version = overrides.pop(
            "source_draft_version",
            self.make_draft_version(question_set=question_set),
        )
        values = {
            "question_set": question_set,
            "revision_number": 1,
            "source_draft_version": source_draft_version,
            "content_hash": "a" * 64,
            "compiled_protocol": {},
            "compiler_version": "1.0.0",
            "engine_version": "1.0.0",
            "created_by": self.account,
        }
        values.update(overrides)
        return QuestionSetRevision.objects.create(**values)

    def test_draft_versions_are_unique_within_draft(self):
        draft = self.make_draft()
        QuestionSetDraftVersion.objects.create(
            draft=draft,
            version_number=1,
            content_hash="a" * 64,
            canonical_body={},
            created_by=self.account,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSetDraftVersion.objects.create(
                draft=draft,
                version_number=1,
                content_hash="b" * 64,
                canonical_body={},
                created_by=self.account,
            )

    def test_team_open_question_set_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_question_set(audience="team", collection_style="open")

    def test_revision_must_compile_from_its_question_sets_draft(self):
        question_set_a = self.make_question_set(title="Question set A")
        source_version_b = self.make_draft_version(
            question_set=self.make_question_set(title="Question set B"),
        )

        assert_trigger_rejection(
            self,
            lambda: QuestionSetRevision.objects.create(
                question_set=question_set_a,
                revision_number=1,
                source_draft_version=source_version_b,
                content_hash="a" * 64,
                compiled_protocol={},
                compiler_version="1.0.0",
                engine_version="1.0.0",
                created_by=self.account,
            ),
            "Question Set Revision source draft version must belong to its Question Set",
        )

    def test_revision_numbers_are_unique_within_question_set(self):
        question_set = self.make_question_set()
        QuestionSetRevision.objects.create(
            question_set=question_set,
            revision_number=1,
            source_draft_version=self.make_draft_version(question_set=question_set),
            content_hash="a" * 64,
            compiled_protocol={},
            compiler_version="1.0.0",
            engine_version="1.0.0",
            created_by=self.account,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSetRevision.objects.create(
                question_set=question_set,
                revision_number=1,
                source_draft_version=self.make_draft_version(
                    question_set=question_set,
                ),
                content_hash="b" * 64,
                compiled_protocol={},
                compiler_version="1.0.0",
                engine_version="1.0.0",
                created_by=self.account,
            )

    def test_authoring_closed_vocabularies_and_hashes_are_database_enforced(self):
        invalid_question_sets = [
            {"audience": "invalid"},
            {"collection_style": "invalid"},
        ]
        for values in invalid_question_sets:
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                self.make_question_set(**values)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_draft(current_version=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_draft_version(change_kind="invalid")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_draft_version(content_hash="A" * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_revision(content_hash="a" * 63)

    def test_question_set_source_revision_must_belong_to_the_same_course(self):
        other_course = Course.objects.create(
            institution=self.institution,
            course_code="other-authoring-course",
            name="Other Authoring Course",
        )
        source_revision = self.make_revision()

        assert_trigger_rejection(
            self,
            lambda: self.make_question_set(
                course=other_course,
                source_question_set_revision=source_revision,
            ),
            "Question Set source revision must belong to the same Course",
        )

    def test_draft_base_revision_must_belong_to_its_question_set(self):
        draft_a = self.make_draft()
        revision_b = self.make_revision(question_set=self.make_question_set())

        assert_trigger_rejection(
            self,
            lambda: QuestionSetDraft.objects.filter(pk=draft_a.pk).update(
                base_revision=revision_b,
            ),
            "Question Set Draft base revision must belong to its Question Set",
        )

    def test_public_ids_receive_postgresql_defaults_for_direct_sql_inserts(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO leai_questionset
                    (course_id, owner_id, title, audience, collection_style, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id, public_id
                """,
                [
                    self.course.pk,
                    self.account.pk,
                    "Direct SQL Question Set",
                    "individual",
                    "guided",
                ],
            )
            question_set_id, question_set_public_id = cursor.fetchone()

        source_draft_version = self.make_draft_version(
            question_set=QuestionSet.objects.get(pk=question_set_id),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO leai_questionsetrevision
                    (question_set_id, revision_number, source_draft_version_id, content_hash,
                     compiled_protocol, compiler_version, engine_version, created_by_id, created_at)
                VALUES (%s, %s, %s, %s, '{}'::jsonb, %s, %s, %s, CURRENT_TIMESTAMP)
                RETURNING public_id
                """,
                [
                    question_set_id,
                    1,
                    source_draft_version.pk,
                    "a" * 64,
                    "1.0.0",
                    "1.0.0",
                    self.account.pk,
                ],
            )
            revision_public_id = cursor.fetchone()[0]

        self.assertIsNotNone(question_set_public_id)
        self.assertIsNotNone(revision_public_id)

    def test_question_set_identity_and_public_ids_are_immutable(self):
        question_set = self.make_question_set()
        other_course = Course.objects.create(
            institution=self.institution,
            course_code="immutable-other-course",
            name="Immutable Other Course",
        )
        question_set.course = other_course
        assert_trigger_rejection(
            self,
            question_set.save,
            "Question Set course, owner, and source revision are immutable",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSet.objects.filter(pk=question_set.pk).update(owner=self.make_account("other"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSet.objects.filter(pk=question_set.pk).update(
                public_id="11111111-1111-4111-8111-111111111111",
            )

        source_revision = self.make_revision()
        sourced_question_set = self.make_question_set(
            source_question_set_revision=source_revision,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSet.objects.filter(pk=sourced_question_set.pk).update(
                source_question_set_revision=None,
            )

    def test_revision_public_id_and_history_rows_are_immutable(self):
        draft_version = self.make_draft_version()
        revision = self.make_revision()

        draft_version.change_kind = "ai"
        with self.assertRaises(IntegrityError), transaction.atomic():
            draft_version.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSetDraftVersion.objects.filter(pk=draft_version.pk).update(
                change_kind="ai",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSetDraftVersion.objects.filter(pk=draft_version.pk).delete()

        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSetRevision.objects.filter(pk=revision.pk).update(
                public_id="22222222-2222-4222-8222-222222222222",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionSetRevision.objects.filter(pk=revision.pk).delete()


class AuthoringConcurrencyTests(TransactionTestCase):
    thread_timeout_seconds = 5

    def setUp(self):
        self.institution = Institution.objects.create(
            slug="authoring-concurrency-institution",
            name="Authoring Concurrency Institution",
        )
        self.course = Course.objects.create(
            institution=self.institution,
            course_code="authoring-concurrency-course",
            name="Authoring Concurrency Course",
        )
        self.other_course = Course.objects.create(
            institution=self.institution,
            course_code="authoring-concurrency-other-course",
            name="Authoring Concurrency Other Course",
        )
        email = "authoring-concurrency@example.edu"
        self.account = InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username="authoring-concurrency",
                email=email,
            ),
            email=email,
            display_name="Authoring Concurrency",
        )

    def make_question_set(self, title):
        return QuestionSet.objects.create(
            course=self.course,
            owner=self.account,
            title=title,
            audience="individual",
            collection_style="guided",
        )

    def make_revision(self, question_set, revision_number=1):
        draft, _ = QuestionSetDraft.objects.get_or_create(
            question_set=question_set,
            defaults={
                "current_version": 1,
                "canonical_body": {},
                "updated_by": self.account,
            },
        )
        draft_version = QuestionSetDraftVersion.objects.create(
            draft=draft,
            version_number=QuestionSetDraftVersion.objects.filter(draft=draft).count() + 1,
            content_hash="a" * 64,
            canonical_body={},
            created_by=self.account,
        )
        return QuestionSetRevision.objects.create(
            question_set=question_set,
            revision_number=revision_number,
            source_draft_version=draft_version,
            content_hash="a" * 64,
            compiled_protocol={},
            compiler_version="1.0.0",
            engine_version="1.0.0",
            created_by=self.account,
        )

    @staticmethod
    def _error_details(error):
        cause = error.__cause__
        return cause.pgcode, cause.diag.message_primary

    def _blocking_pids(self, database_pid):
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_blocking_pids(%s)", [database_pid])
            return cursor.fetchone()[0], connection.connection.get_backend_pid()

    def assert_valid_child_blocks_on_referenced_parent(
        self,
        *,
        parent_model,
        parent_pk,
        child_write,
    ):
        locker_ready = threading.Event()
        child_started = threading.Event()
        child_finished = threading.Event()
        release_locker = threading.Event()
        locker_pid_queue = queue.Queue()
        child_pid_queue = queue.Queue()
        outcomes = queue.Queue()

        def locker():
            database_pid = None
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                database_pid = database.connection.get_backend_pid()
                with transaction.atomic():
                    parent_model.objects.select_for_update().get(pk=parent_pk)
                    locker_pid_queue.put(database_pid)
                    locker_ready.set()
                    if not release_locker.wait(self.thread_timeout_seconds):
                        raise RuntimeError("test did not release the locker transaction")
                outcomes.put(("locker", "committed", database_pid))
            except Exception as error:
                outcomes.put(("locker", "error", database_pid, repr(error)))
            finally:
                close_old_connections()

        def child():
            database_pid = None
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                database_pid = database.connection.get_backend_pid()
                child_pid_queue.put(database_pid)
                with transaction.atomic():
                    if not locker_ready.wait(self.thread_timeout_seconds):
                        raise RuntimeError("locker did not acquire the referenced parent row")
                    child_started.set()
                    child_write()
                outcomes.put(("child", "committed", database_pid))
            except IntegrityError as error:
                outcomes.put(("child", "integrity", database_pid, *self._error_details(error)))
            except DatabaseError as error:
                outcomes.put(("child", "database_error", database_pid, *self._error_details(error)))
            except Exception as error:
                outcomes.put(("child", "error", database_pid, repr(error)))
            finally:
                child_finished.set()
                close_old_connections()

        locker_thread = threading.Thread(target=locker)
        child_thread = threading.Thread(target=child)
        locker_thread.start()
        self.assertTrue(locker_ready.wait(self.thread_timeout_seconds))
        locker_pid = locker_pid_queue.get(timeout=self.thread_timeout_seconds)
        child_thread.start()
        child_pid = child_pid_queue.get(timeout=self.thread_timeout_seconds)
        self.assertTrue(child_started.wait(self.thread_timeout_seconds))

        try:
            for _ in range(100):
                blocking_pids, observer_pid = self._blocking_pids(child_pid)
                if locker_pid in blocking_pids:
                    break
                self.assertFalse(child_finished.is_set(), "child completed without blocking")
            else:
                self.fail("child did not block on the referenced parent row")

            self.assertFalse(child_finished.is_set(), "child produced an outcome before release")
            self.assertTrue(outcomes.empty(), "child produced an outcome before release")
            self.assertNotEqual(locker_pid, child_pid)
            self.assertNotEqual(locker_pid, observer_pid)
            self.assertNotEqual(child_pid, observer_pid)
        finally:
            release_locker.set()
            locker_thread.join(self.thread_timeout_seconds)
            child_thread.join(self.thread_timeout_seconds)

        self.assertFalse(locker_thread.is_alive(), "locker thread did not finish")
        self.assertFalse(child_thread.is_alive(), "child thread did not finish")
        results = {
            outcome[0]: outcome[1:]
            for outcome in [
                outcomes.get(timeout=self.thread_timeout_seconds),
                outcomes.get(timeout=self.thread_timeout_seconds),
            ]
        }
        self.assertEqual(results["locker"][0], "committed", results)
        self.assertEqual(results["child"][0], "committed", results)

    def assert_opposite_order_invalid_writes_reject_without_deadlock(
        self,
        *,
        first_write,
        second_write,
        message,
    ):
        start_writes = threading.Barrier(2)
        outcomes = queue.Queue()

        def write_in_transaction(name, write):
            database_pid = None
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                database_pid = database.connection.get_backend_pid()
                with transaction.atomic():
                    start_writes.wait(self.thread_timeout_seconds)
                    write()
                outcomes.put((name, "committed", database_pid))
            except IntegrityError as error:
                outcomes.put((name, "integrity", database_pid, *self._error_details(error)))
            except DatabaseError as error:
                outcomes.put((name, "database_error", database_pid, *self._error_details(error)))
            except Exception as error:
                outcomes.put((name, "error", database_pid, repr(error)))
            finally:
                close_old_connections()

        first_thread = threading.Thread(target=write_in_transaction, args=("first", first_write))
        second_thread = threading.Thread(target=write_in_transaction, args=("second", second_write))
        first_thread.start()
        second_thread.start()
        first_thread.join(self.thread_timeout_seconds)
        second_thread.join(self.thread_timeout_seconds)

        self.assertFalse(first_thread.is_alive(), "first stress thread did not finish")
        self.assertFalse(second_thread.is_alive(), "second stress thread did not finish")
        results = {
            outcome[0]: outcome[1:]
            for outcome in [
                outcomes.get(timeout=self.thread_timeout_seconds),
                outcomes.get(timeout=self.thread_timeout_seconds),
            ]
        }
        for result in results.values():
            self.assertEqual(result[0], "integrity", results)
            self.assertEqual(result[2], "23514", results)
            self.assertEqual(result[3], message, results)

    def test_source_revision_write_blocks_on_its_referenced_revision(self):
        source_question_set = self.make_question_set("Source")
        source_revision = self.make_revision(source_question_set)

        self.assert_valid_child_blocks_on_referenced_parent(
            parent_model=QuestionSetRevision,
            parent_pk=source_revision.pk,
            child_write=lambda: QuestionSet.objects.create(
                course=self.course,
                owner=self.account,
                source_question_set_revision=source_revision,
                title="Copy",
                audience="individual",
                collection_style="guided",
            ),
        )

        self.assertTrue(
            QuestionSet.objects.filter(
                source_question_set_revision=source_revision,
                course=self.course,
            ).exists(),
        )

    def test_base_revision_write_blocks_on_its_referenced_revision(self):
        question_set = self.make_question_set("Draft owner")
        draft = QuestionSetDraft.objects.create(
            question_set=question_set,
            current_version=1,
            canonical_body={},
            updated_by=self.account,
        )
        base_revision = self.make_revision(question_set)

        self.assert_valid_child_blocks_on_referenced_parent(
            parent_model=QuestionSetRevision,
            parent_pk=base_revision.pk,
            child_write=lambda: QuestionSetDraft.objects.filter(pk=draft.pk).update(
                base_revision=base_revision,
            ),
        )

        draft.refresh_from_db()
        self.assertEqual(draft.base_revision, base_revision)

    def test_revision_source_write_blocks_on_its_referenced_draft_version(self):
        question_set = self.make_question_set("Revision owner")
        draft = QuestionSetDraft.objects.create(
            question_set=question_set,
            current_version=1,
            canonical_body={},
            updated_by=self.account,
        )
        source_draft_version = QuestionSetDraftVersion.objects.create(
            draft=draft,
            version_number=1,
            content_hash="a" * 64,
            canonical_body={},
            created_by=self.account,
        )

        self.assert_valid_child_blocks_on_referenced_parent(
            parent_model=QuestionSetDraftVersion,
            parent_pk=source_draft_version.pk,
            child_write=lambda: QuestionSetRevision.objects.create(
                question_set=question_set,
                revision_number=1,
                source_draft_version=source_draft_version,
                content_hash="a" * 64,
                compiled_protocol={},
                compiler_version="1.0.0",
                engine_version="1.0.0",
                created_by=self.account,
            ),
        )

        self.assertTrue(
            QuestionSetRevision.objects.filter(
                question_set=question_set,
                source_draft_version=source_draft_version,
            ).exists(),
        )

    def test_opposite_order_base_revision_writes_reject_without_deadlock(self):
        question_set_a = self.make_question_set("Base A")
        question_set_b = self.make_question_set("Base B")
        revision_a = self.make_revision(question_set_a)
        revision_b = self.make_revision(question_set_b)

        for _ in range(8):
            self.assert_opposite_order_invalid_writes_reject_without_deadlock(
                first_write=lambda: QuestionSetDraft.objects.filter(
                    pk=question_set_a.draft.pk,
                ).update(base_revision=revision_b),
                second_write=lambda: QuestionSetDraft.objects.filter(
                    pk=question_set_b.draft.pk,
                ).update(base_revision=revision_a),
                message="Question Set Draft base revision must belong to its Question Set",
            )

    def test_opposite_order_revision_source_writes_reject_without_deadlock(self):
        question_set_a = self.make_question_set("Revision A")
        question_set_b = self.make_question_set("Revision B")
        draft_a = QuestionSetDraft.objects.create(
            question_set=question_set_a,
            current_version=1,
            canonical_body={},
            updated_by=self.account,
        )
        draft_b = QuestionSetDraft.objects.create(
            question_set=question_set_b,
            current_version=1,
            canonical_body={},
            updated_by=self.account,
        )
        draft_version_a = QuestionSetDraftVersion.objects.create(
            draft=draft_a,
            version_number=1,
            content_hash="a" * 64,
            canonical_body={},
            created_by=self.account,
        )
        draft_version_b = QuestionSetDraftVersion.objects.create(
            draft=draft_b,
            version_number=1,
            content_hash="a" * 64,
            canonical_body={},
            created_by=self.account,
        )

        for _ in range(8):
            self.assert_opposite_order_invalid_writes_reject_without_deadlock(
                first_write=lambda: QuestionSetRevision.objects.create(
                    question_set=question_set_a,
                    revision_number=1,
                    source_draft_version=draft_version_b,
                    content_hash="a" * 64,
                    compiled_protocol={},
                    compiler_version="1.0.0",
                    engine_version="1.0.0",
                    created_by=self.account,
                ),
                second_write=lambda: QuestionSetRevision.objects.create(
                    question_set=question_set_b,
                    revision_number=1,
                    source_draft_version=draft_version_a,
                    content_hash="a" * 64,
                    compiled_protocol={},
                    compiler_version="1.0.0",
                    engine_version="1.0.0",
                    created_by=self.account,
                ),
                message="Question Set Revision source draft version must belong to its Question Set",
            )
