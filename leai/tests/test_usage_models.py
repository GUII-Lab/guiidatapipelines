import uuid

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import DataError, IntegrityError, connection, transaction
from django.test import TestCase

from leai.models import (
    Course, Institution, InstructorAccount, PdfImportBatch, QuestionSet,
    QuestionSetDraft, QuestionSetDraftVersion, QuestionSetRevision,
    ResponseSession, SurveyOccurrence,
)


class ProductUsageEventModelTests(TestCase):
    def setUp(self):
        institution = Institution.objects.create(slug="usage-test", name="Usage Test")
        course = Course.objects.create(
            institution=institution, course_code="usage-test", name="Usage Test"
        )
        user = get_user_model().objects.create_user(username="usage-test")
        actor = InstructorAccount.objects.create(
            user=user, email="usage@example.edu", display_name="Usage Test"
        )
        question_set = QuestionSet.objects.create(
            course=course, owner=actor, title="Reflection",
            audience="individual", collection_style="guided",
        )
        draft = QuestionSetDraft.objects.create(
            question_set=question_set, updated_by=actor, canonical_body={},
        )
        draft_version = QuestionSetDraftVersion.objects.create(
            draft=draft, version_number=1, content_hash="a" * 64,
            canonical_body={}, change_kind="manual", created_by=actor,
        )
        revision = QuestionSetRevision.objects.create(
            question_set=question_set, revision_number=1,
            source_draft_version=draft_version, content_hash="a" * 64,
            compiled_protocol={}, compiler_version="v1", engine_version="v1",
            created_by=actor,
        )
        self.occurrence = SurveyOccurrence.objects.create(
            revision=revision, course=course, created_by=actor, label="Week 1",
        )
        self.actor = actor

    @property
    def event_model(self):
        return apps.get_model("leai", "ProductUsageEvent")

    def make_session(self, *, consent=True, source="student"):
        if source == "pdf":
            batch = PdfImportBatch.objects.create(
                occurrence=self.occurrence, committed_by=self.actor,
                idempotency_key_hash="b" * 64, manifest_digest="c" * 64,
            )
            return ResponseSession.objects.create(
                occurrence=self.occurrence, pdf_import_batch=batch,
                source="pdf", research_consent=consent,
            )
        return ResponseSession.objects.create(
            occurrence=self.occurrence, source="student",
            capability_nonce="usage-nonce", capability_digest="d" * 64,
            capability_key_version=1, research_consent=consent,
        )

    def make_load(self, session, **overrides):
        values = dict(
            response_session=session, event_type="survey_loaded", event_version=1,
            response_phase="first_load", app_build_sha="abc1234",
        )
        values.update(overrides)
        return self.event_model.objects.create(**values)

    def make_download(self, session, **overrides):
        values = dict(
            response_session=session, event_type="output_download_attempted",
            event_version=1, output_kind="certificate", outcome="succeeded",
            app_build_sha="abc1234",
        )
        values.update(overrides)
        return self.event_model.objects.create(**values)

    def test_all_four_load_phases_have_only_load_fields(self):
        session = self.make_session()
        for phase in (
            "first_load", "reload_before_response", "resume_during_response",
            "reload_after_completion",
        ):
            with self.subTest(phase=phase):
                event = self.make_load(session, response_phase=phase)
                self.assertEqual(event.response_phase, phase)
                self.assertIsNone(event.output_kind)
                self.assertIsNone(event.outcome)
                self.assertIsNotNone(event.occurred_at)

    def test_both_download_kinds_and_four_outcomes_are_accepted(self):
        session = self.make_session()
        for kind in ("certificate", "completed_response"):
            for outcome in ("succeeded", "not_ready", "closed", "generation_failed"):
                with self.subTest(kind=kind, outcome=outcome):
                    event = self.make_download(session, output_kind=kind, outcome=outcome)
                    self.assertEqual((event.output_kind, event.outcome), (kind, outcome))
                    self.assertIsNone(event.response_phase)

    def test_load_requires_phase_and_forbids_download_fields(self):
        session = self.make_session()
        for overrides in (
            {"response_phase": None}, {"response_phase": "unknown"},
            {"output_kind": "certificate"}, {"outcome": "succeeded"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(IntegrityError), transaction.atomic():
                self.make_load(session, **overrides)

    def test_download_requires_kind_and_outcome_and_forbids_phase(self):
        session = self.make_session()
        for overrides in (
            {"output_kind": None}, {"output_kind": "unknown"},
            {"outcome": None}, {"outcome": "unknown"},
            {"response_phase": "first_load"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(IntegrityError), transaction.atomic():
                self.make_download(session, **overrides)

    def test_only_two_event_types_and_version_one_are_accepted(self):
        session = self.make_session()
        for overrides in (
            {"event_type": "page_view"}, {"event_type": ""},
            {"event_version": 0}, {"event_version": 2},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(IntegrityError), transaction.atomic():
                self.make_load(session, **overrides)

    def test_sha_must_be_lowercase_hex_between_seven_and_sixty_four_chars(self):
        session = self.make_session()
        for sha in ("a" * 6, "a" * 65, "ABC1234", "abc123g", "abc 123"):
            expected_error = DataError if len(sha) > 64 else IntegrityError
            with self.subTest(sha=sha), self.assertRaises(expected_error), transaction.atomic():
                self.make_load(session, app_build_sha=sha)
        for sha in ("a" * 7, "a" * 64):
            with self.subTest(sha=sha):
                self.assertEqual(self.make_load(session, app_build_sha=sha).app_build_sha, sha)

    def test_parent_consent_is_authoritative_at_insert(self):
        session = self.make_session(consent=False)
        for creator in (self.make_load, self.make_download):
            with self.subTest(creator=creator.__name__), self.assertRaises(IntegrityError), transaction.atomic():
                creator(session)
        self.assertEqual(self.event_model.objects.count(), 0)

    def test_pdf_session_cannot_emit_even_if_consent_flag_is_true(self):
        session = self.make_session(consent=True, source="pdf")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_load(session)

    def test_duplicate_event_id_is_unique(self):
        session = self.make_session()
        event_id = uuid.uuid4()
        self.make_load(session, event_id=event_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_download(session, event_id=event_id)
        self.assertEqual(self.event_model.objects.count(), 1)

    def test_database_generates_event_uuid_when_omitted_by_raw_insert(self):
        session = self.make_session()
        table = self.event_model._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {table} (response_session_id, event_type, event_version, "
                "response_phase, app_build_sha, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s, NOW()) RETURNING event_id",
                [session.pk, "survey_loaded", 1, "first_load", "abc1234"],
            )
            event_id = cursor.fetchone()[0]
        self.assertIsInstance(event_id, uuid.UUID)

    def test_direct_queryset_update_and_delete_are_rejected(self):
        session = self.make_session()
        event = self.make_load(session)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.event_model.objects.filter(pk=event.pk).update(response_phase="reload_before_response")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.event_model.objects.filter(pk=event.pk).delete()
        self.assertEqual(self.event_model.objects.get(pk=event.pk).response_phase, "first_load")

    def test_direct_sql_truncate_is_rejected(self):
        session = self.make_session()
        event = self.make_load(session)
        connection.check_constraints()
        table = self.event_model._meta.db_table
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"TRUNCATE TABLE {table}")
        self.assertTrue(self.event_model.objects.filter(pk=event.pk).exists())

    def test_model_has_only_bounded_schema_columns(self):
        self.assertEqual(
            {field.name for field in self.event_model._meta.local_fields},
            {
                "id", "event_id", "response_session", "event_type", "event_version",
                "response_phase", "output_kind", "outcome", "app_build_sha", "occurred_at",
            },
        )
