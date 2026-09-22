import queue
import threading
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from leai.models import (
    AuthoringConversation,
    AuthoringMessage,
    AuthoringRun,
    Course,
    Institution,
    InstructorAccount,
    PreviewDecision,
    PreviewMessage,
    PreviewSession,
    QuestionSet,
    QuestionSetDraft,
    QuestionSetDraftVersion,
    QuestionSetRevision,
    QuestionSetTemplate,
    QuestionSetTemplateRevision,
    SurveyOccurrence,
)


def assert_trigger_rejection(test_case, write, message=None, sqlstate=None):
    with test_case.assertRaises(IntegrityError) as raised, transaction.atomic():
        write()
    if message or sqlstate:
        cause = raised.exception.__cause__
        test_case.assertEqual(cause.pgcode, sqlstate or "23514")
    if message:
        test_case.assertEqual(cause.diag.message_primary, message)


class PublicationModelTests(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(
            slug="publication-test-institution",
            name="Publication Test Institution",
        )
        self.course = self.make_course("publication-test-course")
        self.account = self.make_account("primary")

    def make_account(self, suffix):
        email = f"publication-{suffix}@example.edu"
        return InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username=f"publication-{suffix}", email=email,
            ),
            email=email,
            display_name=f"Publication {suffix}",
        )

    def make_course(self, code):
        return Course.objects.create(
            institution=self.institution,
            course_code=code,
            name=code.replace("-", " ").title(),
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

    def make_draft_version(self, **overrides):
        question_set = overrides.pop("question_set", None) or self.make_question_set()
        draft, _ = QuestionSetDraft.objects.get_or_create(
            question_set=question_set,
            defaults={
                "current_version": 1,
                "canonical_body": {},
                "updated_by": self.account,
            },
        )
        values = {
            "draft": draft,
            "version_number": QuestionSetDraftVersion.objects.filter(draft=draft).count() + 1,
            "content_hash": "a" * 64,
            "canonical_body": {},
            "created_by": self.account,
        }
        values.update(overrides)
        return QuestionSetDraftVersion.objects.create(**values)

    def make_revision(self, **overrides):
        question_set = overrides.pop("question_set", None) or self.make_question_set()
        values = {
            "question_set": question_set,
            "revision_number": QuestionSetRevision.objects.filter(
                question_set=question_set,
            ).count() + 1,
            "source_draft_version": self.make_draft_version(question_set=question_set),
            "content_hash": "a" * 64,
            "compiled_protocol": {},
            "compiler_version": "1.0.0",
            "engine_version": "1.0.0",
            "created_by": self.account,
        }
        values.update(overrides)
        return QuestionSetRevision.objects.create(**values)

    def make_conversation(self, **overrides):
        values = {
            "question_set": self.make_question_set(),
            "origin_surface": "builder",
            "created_by": self.account,
        }
        values.update(overrides)
        return AuthoringConversation.objects.create(**values)

    def make_template(self, **overrides):
        values = {"title": "Private reflection template", "owner_account": self.account}
        values.update(overrides)
        return QuestionSetTemplate.objects.create(**values)

    def make_template_revision(self, **overrides):
        template = overrides.pop("template", None) or self.make_template()
        values = {
            "template": template,
            "revision_number": QuestionSetTemplateRevision.objects.filter(
                template=template,
            ).count() + 1,
            "content_hash": "b" * 64,
            "canonical_body": {},
            "created_by": self.account,
        }
        values.update(overrides)
        return QuestionSetTemplateRevision.objects.create(**values)

    def make_occurrence(self, **overrides):
        revision = overrides.pop("revision", None) or self.make_revision()
        values = {
            "revision": revision,
            "course": revision.question_set.course,
            "created_by": self.account,
            "label": "Week 1 reflection",
            "provenance": "native",
            "management_mode": "managed",
            "settings_version": 1,
        }
        values.update(overrides)
        return SurveyOccurrence.objects.create(**values)

    def test_team_open_rejection_is_inherited_from_question_set(self):
        assert_trigger_rejection(
            self,
            lambda: self.make_question_set(audience="team", collection_style="open"),
        )

    def test_conversation_is_one_per_question_set_and_messages_have_valid_shape(self):
        conversation = self.make_conversation()
        self.assertIn(
            "source_analysis_snapshot",
            {field.name for field in AuthoringConversation._meta.get_fields()},
        )
        self.assertIsNone(conversation.source_analysis_snapshot)
        assert_trigger_rejection(
            self,
            lambda: self.make_conversation(question_set=conversation.question_set),
        )
        assert_trigger_rejection(
            self,
            lambda: self.make_conversation(origin_surface="unknown"),
        )

        AuthoringMessage.objects.create(
            conversation=conversation,
            sequence=1,
            role="user",
            input_method="typed",
            content="Can you make this more reflective?",
        )
        for values in (
            {"sequence": 1, "role": "assistant", "input_method": None},
            {"sequence": 2, "role": "user", "input_method": None},
            {"sequence": 2, "role": "assistant", "input_method": "voice"},
            {"sequence": 2, "role": "invalid", "input_method": None},
            {"sequence": 2, "role": "user", "input_method": "invalid"},
        ):
            with self.subTest(values=values):
                assert_trigger_rejection(
                    self,
                    lambda values=values: AuthoringMessage.objects.create(
                        conversation=conversation,
                        content="Invalid message",
                        **values,
                    ),
                )

    def test_run_uses_its_conversations_question_set_and_bounded_json(self):
        conversation = self.make_conversation()
        matching_version = self.make_draft_version(question_set=conversation.question_set)
        AuthoringRun.objects.create(
            conversation=conversation,
            base_draft_version=matching_version,
            requested_by=self.account,
            status="pending",
            source_provenance_snapshot={"source": "builder"},
        )
        assert_trigger_rejection(
            self,
            lambda: AuthoringRun.objects.create(
                conversation=conversation,
                base_draft_version=self.make_draft_version(),
                requested_by=self.account,
                status="pending",
                source_provenance_snapshot={"source": "builder"},
            ),
            "Authoring Run base draft version must belong to its Conversation Question Set",
        )
        assert_trigger_rejection(
            self,
            lambda: AuthoringConversation.objects.filter(pk=conversation.pk).update(
                question_set=self.make_question_set(),
            ),
            "Authoring Conversation creation provenance is immutable",
        )
        for values in (
            {"status": "invalid", "source_provenance_snapshot": {"source": "builder"}},
            {"status": "pending", "source_provenance_snapshot": []},
            {"status": "pending", "source_provenance_snapshot": {"source": "x" * 20000}},
        ):
            with self.subTest(values=values):
                assert_trigger_rejection(
                    self,
                    lambda values=values: AuthoringRun.objects.create(
                        conversation=conversation,
                        base_draft_version=matching_version,
                        requested_by=self.account,
                        **values,
                    ),
                )

    def test_template_has_exactly_one_owner_and_revision_lineage_is_valid(self):
        assert_trigger_rejection(
            self,
            lambda: QuestionSetTemplate.objects.create(title="No owner"),
        )
        assert_trigger_rejection(
            self,
            lambda: QuestionSetTemplate.objects.create(
                title="Two owners",
                owner_account=self.account,
                owner_institution=self.institution,
            ),
        )
        private_template = self.make_template()
        institution_template = self.make_template(owner_account=None, owner_institution=self.institution)
        private_revision = self.make_template_revision(
            template=private_template,
            source_question_set_revision=self.make_revision(),
        )
        institution_source_revision = self.make_revision()
        self.make_template_revision(
            template=institution_template,
            source_question_set_revision=institution_source_revision,
        )

        assert_trigger_rejection(
            self,
            lambda: self.make_template_revision(
                template=private_template,
                source_question_set_revision=self.make_revision(
                    question_set=self.make_question_set(owner=self.make_account("other")),
                ),
            ),
            "Template source revision must match its owner scope",
        )
        other_institution = Institution.objects.create(
            slug="publication-other-institution",
            name="Publication Other Institution",
        )
        other_institution_course = Course.objects.create(
            institution=other_institution,
            course_code="publication-other-institution-course",
            name="Publication Other Institution Course",
        )
        assert_trigger_rejection(
            self,
            lambda: self.make_template_revision(
                template=institution_template,
                source_question_set_revision=self.make_revision(
                    question_set=self.make_question_set(course=other_institution_course),
                ),
            ),
            "Template source revision must match its owner scope",
        )
        assert_trigger_rejection(
            self,
            lambda: QuestionSetTemplateRevision.objects.filter(pk=private_revision.pk).update(
                canonical_body={"changed": True},
            ),
            "Template revisions are immutable",
        )
        assert_trigger_rejection(
            self,
            lambda: QuestionSetTemplate.objects.filter(pk=private_template.pk).update(
                owner_account=self.make_account("replacement-owner"),
            ),
            "Template source revision must match its owner scope",
        )
        assert_trigger_rejection(
            self,
            lambda: Course.objects.filter(
                pk=institution_source_revision.question_set.course_id,
            ).update(institution=other_institution),
            "Course institution must match dependent institution template revisions",
        )
        assert_trigger_rejection(
            self,
            lambda: QuestionSetTemplateRevision.objects.filter(pk=private_revision.pk).delete(),
            "Template revisions are immutable",
        )

    def test_preview_rows_are_isolated_and_decision_is_one_immutable_gate(self):
        revision = self.make_revision()
        preview = PreviewSession.objects.create(
            revision=revision,
            actor=self.account,
            capability_digest="c" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assert_trigger_rejection(
            self,
            lambda: PreviewSession.objects.create(
                revision=revision,
                actor=self.account,
                capability_digest="not-a-digest",
                expires_at=timezone.now() + timedelta(hours=1),
            ),
        )
        PreviewMessage.objects.create(
            preview_session=preview,
            sequence=1,
            role="student",
            content="I noticed my approach changed.",
            attribution={"kind": "preview"},
        )
        self.assertNotIn(
            "response_session",
            {field.name for field in PreviewMessage._meta.get_fields()},
        )
        assert_trigger_rejection(
            self,
            lambda: PreviewMessage.objects.create(
                preview_session=preview,
                sequence=1,
                role="assistant",
                content="Duplicate sequence",
                attribution={},
            ),
        )
        assert_trigger_rejection(
            self,
            lambda: PreviewMessage.objects.create(
                preview_session=preview,
                sequence=2,
                role="invalid",
                content="Invalid role",
                attribution={},
            ),
        )
        decision = PreviewDecision.objects.create(
            revision=revision,
            actor=self.account,
            decision="completed",
            idempotency_key_hash="d" * 64,
        )
        assert_trigger_rejection(
            self,
            lambda: PreviewDecision.objects.create(
                revision=self.make_revision(),
                actor=self.account,
                decision="invalid",
                idempotency_key_hash="short",
            ),
        )
        assert_trigger_rejection(
            self,
            lambda: PreviewDecision.objects.create(
                revision=revision,
                actor=self.account,
                decision="skipped",
                idempotency_key_hash="e" * 64,
            ),
        )
        assert_trigger_rejection(
            self,
            lambda: PreviewDecision.objects.filter(pk=decision.pk).update(decision="skipped"),
            "Preview decisions are immutable",
        )
        assert_trigger_rejection(
            self,
            lambda: PreviewDecision.objects.filter(pk=decision.pk).delete(),
            "Preview decisions are immutable",
        )

    def test_occurrence_course_schedule_and_identity_are_database_enforced(self):
        occurrence = self.make_occurrence()
        other_course = self.make_course("publication-other-course")
        assert_trigger_rejection(
            self,
            lambda: self.make_occurrence(course=other_course),
            "Survey Occurrence course must match its Revision Question Set course",
        )
        assert_trigger_rejection(
            self,
            lambda: self.make_occurrence(
                opens_at=timezone.now() + timedelta(days=1),
                closes_at=timezone.now(),
            ),
        )
        for values in (
            {"provenance": "invalid"},
            {"management_mode": "invalid"},
            {"settings_version": 0},
        ):
            with self.subTest(values=values):
                assert_trigger_rejection(self, lambda values=values: self.make_occurrence(**values))

        other_account = self.make_account("occurrence-other")
        other_revision = self.make_revision()
        for values in (
            {"public_id": "11111111-1111-4111-8111-111111111111"},
            {"revision": other_revision},
            {"course": other_course},
            {"created_by": other_account},
            {"provenance": "imported"},
            {"management_mode": "imported_read_only"},
        ):
            with self.subTest(values=values):
                assert_trigger_rejection(
                    self,
                    lambda values=values: SurveyOccurrence.objects.filter(
                        pk=occurrence.pk,
                    ).update(**values),
                    "Survey Occurrence identity is immutable",
                )
        SurveyOccurrence.objects.filter(pk=occurrence.pk).update(
            label="Week 1 revised",
            settings_version=2,
            completion_certificate_enabled=True,
            completed_response_download_enabled=True,
            opens_at=timezone.now(),
            closes_at=timezone.now() + timedelta(days=7),
            manually_closed_at=timezone.now() + timedelta(days=1),
        )
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.label, "Week 1 revised")
        self.assertEqual(occurrence.settings_version, 2)
        self.assertTrue(occurrence.completion_certificate_enabled)
        self.assertTrue(occurrence.completed_response_download_enabled)
        self.assertIsNotNone(occurrence.opens_at)
        self.assertIsNotNone(occurrence.closes_at)
        self.assertIsNotNone(occurrence.manually_closed_at)

    def test_externally_addressed_rows_get_database_uuid_defaults_on_direct_write(self):
        revision = self.make_revision()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO leai_questionsettemplate
                    (title, owner_account_id, created_at, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id, public_id
                """,
                ["Direct template", self.account.pk],
            )
            template_id, template_public_id = cursor.fetchone()
            cursor.execute(
                """
                INSERT INTO leai_questionsettemplaterevision
                    (template_id, revision_number, source_question_set_revision_id,
                     content_hash, canonical_body, created_by_id, created_at)
                VALUES (%s, %s, %s, %s, '{}'::jsonb, %s, CURRENT_TIMESTAMP)
                RETURNING public_id
                """,
                [template_id, 1, revision.pk, "b" * 64, self.account.pk],
            )
            template_revision_public_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO leai_previewsession
                    (revision_id, actor_id, capability_digest, expires_at, created_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP + INTERVAL '1 hour', CURRENT_TIMESTAMP)
                RETURNING public_id
                """,
                [revision.pk, self.account.pk, "e" * 64],
            )
            preview_public_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO leai_surveyoccurrence
                    (revision_id, course_id, created_by_id, label, provenance, management_mode,
                     settings_version, completion_certificate_enabled,
                     completed_response_download_enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, false, false,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING public_id
                """,
                [
                    revision.pk,
                    self.course.pk,
                    self.account.pk,
                    "Direct occurrence",
                    "native",
                    "managed",
                    1,
                ],
            )
            occurrence_public_id = cursor.fetchone()[0]
        self.assertIsNotNone(template_public_id)
        self.assertIsNotNone(template_revision_public_id)
        self.assertIsNotNone(preview_public_id)
        self.assertIsNotNone(occurrence_public_id)
        for model, public_id in (
            (QuestionSetTemplate, template_public_id),
            (QuestionSetTemplateRevision, template_revision_public_id),
            (PreviewSession, preview_public_id),
            (SurveyOccurrence, occurrence_public_id),
        ):
            with self.subTest(model=model.__name__):
                assert_trigger_rejection(
                    self,
                    lambda model=model, public_id=public_id: model.objects.filter(
                        public_id=public_id,
                    ).update(public_id="33333333-3333-4333-8333-333333333333"),
                    sqlstate="23514",
                )


class PublicationConcurrencyTests(TransactionTestCase):
    thread_timeout_seconds = 5

    def setUp(self):
        self.institution = Institution.objects.create(
            slug="publication-concurrency-institution",
            name="Publication Concurrency Institution",
        )
        self.course = Course.objects.create(
            institution=self.institution,
            course_code="publication-concurrency-course",
            name="Publication Concurrency Course",
        )
        email = "publication-concurrency@example.edu"
        self.account = InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username="publication-concurrency", email=email,
            ),
            email=email,
            display_name="Publication Concurrency",
        )

    def make_revision(self):
        question_set = QuestionSet.objects.create(
            course=self.course,
            owner=self.account,
            title="Concurrency questions",
            audience="individual",
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
            content_hash="a" * 64,
            canonical_body={},
            created_by=self.account,
        )
        return QuestionSetRevision.objects.create(
            question_set=question_set,
            revision_number=1,
            source_draft_version=draft_version,
            content_hash="a" * 64,
            compiled_protocol={},
            compiler_version="1.0.0",
            engine_version="1.0.0",
            created_by=self.account,
        )

    def make_question_set(self, title):
        return QuestionSet.objects.create(
            course=self.course,
            owner=self.account,
            title=title,
            audience="individual",
            collection_style="guided",
        )

    def assert_parent_first_child_write_stays_consistent(
        self,
        *,
        parent_write,
        child_write,
        child_message,
    ):
        parent_ready = threading.Event()
        release_parent = threading.Event()
        child_started = threading.Event()
        child_finished = threading.Event()
        outcomes = queue.Queue()

        def parent():
            try:
                close_old_connections()
                with transaction.atomic():
                    parent_write()
                    parent_ready.set()
                    if not release_parent.wait(self.thread_timeout_seconds):
                        raise RuntimeError("parent was not released")
                outcomes.put(("parent", "committed"))
            except Exception as error:
                outcomes.put(("parent", "error", repr(error)))
            finally:
                close_old_connections()

        def child():
            try:
                close_old_connections()
                if not parent_ready.wait(self.thread_timeout_seconds):
                    raise RuntimeError("parent mutation did not begin")
                with transaction.atomic():
                    child_started.set()
                    child_write()
                outcomes.put(("child", "committed"))
            except IntegrityError as error:
                cause = error.__cause__
                outcomes.put(("child", "integrity", cause.pgcode, cause.diag.message_primary))
            except Exception as error:
                outcomes.put(("child", "error", repr(error)))
            finally:
                child_finished.set()
                close_old_connections()

        parent_thread = threading.Thread(target=parent)
        child_thread = threading.Thread(target=child)
        parent_thread.start()
        self.assertTrue(parent_ready.wait(self.thread_timeout_seconds))
        child_thread.start()
        self.assertTrue(child_started.wait(self.thread_timeout_seconds))
        self.assertFalse(child_finished.wait(0.1), "child did not block on the mutated parent")
        release_parent.set()
        parent_thread.join(self.thread_timeout_seconds)
        child_thread.join(self.thread_timeout_seconds)

        self.assertFalse(parent_thread.is_alive())
        self.assertFalse(child_thread.is_alive())
        results = {result[0]: result[1:] for result in (outcomes.get(), outcomes.get())}
        self.assertEqual(results["parent"], ("committed",), results)
        self.assertEqual(results["child"], ("integrity", "23514", child_message), results)

    def assert_child_first_parent_mutation_rejects(
        self,
        *,
        child_write,
        parent_write,
        parent_message,
    ):
        child_written = threading.Event()
        release_child = threading.Event()
        parent_started = threading.Event()
        parent_finished = threading.Event()
        outcomes = queue.Queue()

        def child():
            try:
                close_old_connections()
                with transaction.atomic():
                    child_write()
                    child_written.set()
                    if not release_child.wait(self.thread_timeout_seconds):
                        raise RuntimeError("child was not released")
                outcomes.put(("child", "committed"))
            except Exception as error:
                outcomes.put(("child", "error", repr(error)))
            finally:
                close_old_connections()

        def parent():
            try:
                close_old_connections()
                if not child_written.wait(self.thread_timeout_seconds):
                    raise RuntimeError("child did not write")
                with transaction.atomic():
                    parent_started.set()
                    parent_write()
                outcomes.put(("parent", "committed"))
            except IntegrityError as error:
                cause = error.__cause__
                outcomes.put(("parent", "integrity", cause.pgcode, cause.diag.message_primary))
            except Exception as error:
                outcomes.put(("parent", "error", repr(error)))
            finally:
                parent_finished.set()
                close_old_connections()

        child_thread = threading.Thread(target=child)
        parent_thread = threading.Thread(target=parent)
        child_thread.start()
        self.assertTrue(child_written.wait(self.thread_timeout_seconds))
        parent_thread.start()
        self.assertTrue(parent_started.wait(self.thread_timeout_seconds))
        self.assertFalse(parent_finished.wait(0.1), "parent did not block on child lineage")
        release_child.set()
        child_thread.join(self.thread_timeout_seconds)
        parent_thread.join(self.thread_timeout_seconds)

        self.assertFalse(child_thread.is_alive())
        self.assertFalse(parent_thread.is_alive())
        results = {result[0]: result[1:] for result in (outcomes.get(), outcomes.get())}
        self.assertEqual(results["child"], ("committed",), results)
        self.assertEqual(results["parent"], ("integrity", "23514", parent_message), results)

    def test_parent_first_course_move_rejects_later_institution_template_source(self):
        revision = self.make_revision()
        template = QuestionSetTemplate.objects.create(
            title="Institution template",
            owner_institution=self.institution,
        )
        other_institution = Institution.objects.create(
            slug="publication-concurrency-other-institution-parent-first",
            name="Publication Concurrency Other Institution Parent First",
        )
        self.assert_parent_first_child_write_stays_consistent(
            parent_write=lambda: Course.objects.filter(pk=self.course.pk).update(
                institution=other_institution,
            ),
            child_write=lambda: QuestionSetTemplateRevision.objects.create(
                template=template,
                revision_number=1,
                source_question_set_revision=revision,
                content_hash="b" * 64,
                canonical_body={},
                created_by=self.account,
            ),
            child_message="Template source revision must match its owner scope",
        )

    def test_child_first_institution_template_source_rejects_later_course_move(self):
        revision = self.make_revision()
        template = QuestionSetTemplate.objects.create(
            title="Institution template",
            owner_institution=self.institution,
        )
        other_institution = Institution.objects.create(
            slug="publication-concurrency-other-institution-child-first",
            name="Publication Concurrency Other Institution Child First",
        )
        self.assert_child_first_parent_mutation_rejects(
            child_write=lambda: QuestionSetTemplateRevision.objects.create(
                template=template,
                revision_number=1,
                source_question_set_revision=revision,
                content_hash="b" * 64,
                canonical_body={},
                created_by=self.account,
            ),
            parent_write=lambda: Course.objects.filter(pk=self.course.pk).update(
                institution=other_institution,
            ),
            parent_message="Course institution must match dependent institution template revisions",
        )

    def test_occurrence_write_blocks_on_referenced_revision(self):
        revision = self.make_revision()
        locker_ready = threading.Event()
        child_started = threading.Event()
        child_finished = threading.Event()
        release_locker = threading.Event()
        pids = queue.Queue()
        outcomes = queue.Queue()

        def locker():
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                with transaction.atomic():
                    QuestionSetRevision.objects.select_for_update().get(pk=revision.pk)
                    pids.put(("locker", database.connection.get_backend_pid()))
                    locker_ready.set()
                    if not release_locker.wait(self.thread_timeout_seconds):
                        raise RuntimeError("locker was not released")
                outcomes.put(("locker", "committed"))
            except Exception as error:
                outcomes.put(("locker", "error", repr(error)))
            finally:
                close_old_connections()

        def create_child():
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                pids.put(("child", database.connection.get_backend_pid()))
                if not locker_ready.wait(self.thread_timeout_seconds):
                    raise RuntimeError("locker did not acquire revision")
                child_started.set()
                with transaction.atomic():
                    SurveyOccurrence.objects.create(
                        revision=revision,
                        course=self.course,
                        created_by=self.account,
                        label="Blocked occurrence",
                        provenance="native",
                        management_mode="managed",
                        settings_version=1,
                    )
                outcomes.put(("child", "committed"))
            except Exception as error:
                outcomes.put(("child", "error", repr(error)))
            finally:
                child_finished.set()
                close_old_connections()

        locker_thread = threading.Thread(target=locker)
        child_thread = threading.Thread(target=create_child)
        locker_thread.start()
        self.assertTrue(locker_ready.wait(self.thread_timeout_seconds))
        locker_thread_name, locker_pid = pids.get(timeout=self.thread_timeout_seconds)
        self.assertEqual(locker_thread_name, "locker")
        child_thread.start()
        child_thread_name, child_pid = pids.get(timeout=self.thread_timeout_seconds)
        self.assertEqual(child_thread_name, "child")
        self.assertTrue(child_started.wait(self.thread_timeout_seconds))

        try:
            for _ in range(100):
                connection.ensure_connection()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_blocking_pids(%s)", [child_pid])
                    blocking_pids = cursor.fetchone()[0]
                if locker_pid in blocking_pids:
                    break
                self.assertFalse(child_finished.is_set(), "child committed without locking revision")
                time.sleep(0.01)
            else:
                self.fail("child did not block on the referenced revision")
        finally:
            release_locker.set()
            locker_thread.join(self.thread_timeout_seconds)
            child_thread.join(self.thread_timeout_seconds)

        self.assertFalse(locker_thread.is_alive())
        self.assertFalse(child_thread.is_alive())
        self.assertEqual(dict(outcomes.get() for _ in range(2)), {"locker": "committed", "child": "committed"})
