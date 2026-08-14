import json
import threading
from io import BytesIO
from unittest import mock

from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase
from pypdf import PdfReader

from datapipeline.leai_completion import (
    eligible_student_message_count,
    generate_code,
    issue_or_get_certificate,
    normalize_code,
    render_certificate_pdf,
    sanitize_progress_snapshot,
)
from datapipeline.models import (
    Course,
    FeedbackGPT,
    FeedbackMessage,
    SurveyCompletionCertificate,
)


def _make_course(course_id="cert-course", course_name="Certificate Course"):
    return Course.objects.create(
        course_id=course_id,
        course_name=course_name,
        instructor_name="Prof. Test",
        password="pw",
    )


def _make_survey(course, public_id="cert-public"):
    return FeedbackGPT.objects.create(
        name="Week 1 Reflection",
        instructions="Be helpful.",
        course=course,
        week_number=1,
        survey_label="Week 1",
        public_id=public_id,
    )


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def _pdf_text(pdf_bytes):
    return "\n".join(
        page.extract_text() or ""
        for page in PdfReader(BytesIO(pdf_bytes)).pages
    )


def _certificate_rows_snapshot():
    field_names = [field.attname for field in SurveyCompletionCertificate._meta.concrete_fields]
    return list(
        SurveyCompletionCertificate.objects.order_by("pk").values(*field_names)
    )


class CompletionCourseSettingsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.course = _make_course()
        self.survey = _make_survey(self.course)

    def test_new_course_defaults_downloads_off(self):
        self.assertFalse(self.course.completion_certificate_enabled)
        self.assertFalse(self.course.parsed_document_download_enabled)

    def test_get_course_customization_includes_download_flags(self):
        response = self.client.get(
            "/datapipeline/api/get_course_customization/",
            {"course_id": self.course.course_id},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("completion_certificate_enabled", payload)
        self.assertIn("parsed_document_download_enabled", payload)
        self.assertFalse(payload["completion_certificate_enabled"])
        self.assertFalse(payload["parsed_document_download_enabled"])

    def test_partial_customization_update_preserves_other_flags(self):
        self.course.parsed_document_download_enabled = True
        self.course.save(update_fields=["parsed_document_download_enabled"])

        response = _post(
            self.client,
            "/datapipeline/api/update_course_customization/",
            {
                "course_id": self.course.course_id,
                "completion_certificate_enabled": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.course.refresh_from_db()
        self.assertTrue(self.course.completion_certificate_enabled)
        self.assertTrue(self.course.parsed_document_download_enabled)
        self.assertTrue(response.json()["completion_certificate_enabled"])
        self.assertTrue(response.json()["parsed_document_download_enabled"])

    def test_partial_customization_update_rejects_non_boolean_download_flags(self):
        cases = [
            ("completion_certificate_enabled", "false"),
            ("completion_certificate_enabled", 0),
            ("parsed_document_download_enabled", None),
            ("parsed_document_download_enabled", {"bad": True}),
        ]

        self.course.completion_certificate_enabled = True
        self.course.parsed_document_download_enabled = False
        self.course.save(update_fields=[
            "completion_certificate_enabled",
            "parsed_document_download_enabled",
        ])

        for field_name, invalid_value in cases:
            with self.subTest(field_name=field_name, invalid_value=invalid_value):
                response = _post(
                    self.client,
                    "/datapipeline/api/update_course_customization/",
                    {
                        "course_id": self.course.course_id,
                        field_name: invalid_value,
                    },
                )

                self.assertEqual(response.status_code, 400)
                self.course.refresh_from_db()
                self.assertTrue(self.course.completion_certificate_enabled)
                self.assertFalse(self.course.parsed_document_download_enabled)

    def test_partial_customization_update_rejects_mixed_valid_and_invalid_download_flags(self):
        self.course.completion_certificate_enabled = False
        self.course.parsed_document_download_enabled = True
        self.course.save(update_fields=[
            "completion_certificate_enabled",
            "parsed_document_download_enabled",
        ])

        response = _post(
            self.client,
            "/datapipeline/api/update_course_customization/",
            {
                "course_id": self.course.course_id,
                "completion_certificate_enabled": True,
                "parsed_document_download_enabled": "false",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.course.refresh_from_db()
        self.assertFalse(self.course.completion_certificate_enabled)
        self.assertTrue(self.course.parsed_document_download_enabled)

    def test_same_survey_session_cannot_have_two_certificates(self):
        SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="session-1",
            code="GUII-2026-ABC-001",
            progress_snapshot={"completed": 12},
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SurveyCompletionCertificate.objects.create(
                    survey=self.survey,
                    session_id="session-1",
                    code="GUII-2026-ABC-002",
                    progress_snapshot={"completed": 12},
                )

    def test_certificate_code_is_globally_unique(self):
        SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="session-1",
            code="GUII-2026-ABC-001",
            progress_snapshot={"completed": 12},
        )
        other_survey = _make_survey(self.course, public_id="cert-public-2")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SurveyCompletionCertificate.objects.create(
                    survey=other_survey,
                    session_id="session-2",
                    code="GUII-2026-ABC-001",
                    progress_snapshot={"completed": 12},
                )

    def test_certificate_str_does_not_expose_private_linkage(self):
        certificate = SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="session-1",
            code="GUII-2026-ABC-003",
            progress_snapshot={"completed": 12},
        )

        rendered = str(certificate)

        self.assertNotIn(certificate.session_id, rendered)
        self.assertNotIn(self.survey.public_id, rendered)
        self.assertNotIn(self.survey.name, rendered)
        self.assertNotIn("completed", rendered)


class PublicBootstrapDownloadFlagsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.course = _make_course(course_id="public-course")
        self.survey = _make_survey(self.course, public_id="publicflags01")

    def test_get_feedback_gpt_by_public_id_includes_course_download_flags(self):
        self.course.completion_certificate_enabled = True
        self.course.parsed_document_download_enabled = True
        self.course.save(update_fields=[
            "completion_certificate_enabled",
            "parsed_document_download_enabled",
        ])

        response = self.client.get(
            "/datapipeline/api/get_feedback_gpt_by_public_id/",
            {"public_id": self.survey.public_id},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["completion_certificate_enabled"])
        self.assertTrue(payload["parsed_document_download_enabled"])

    def test_get_feedback_gpt_by_public_id_defaults_download_flags_false_without_course(self):
        survey = FeedbackGPT.objects.create(
            name="Standalone Survey",
            instructions="Be helpful.",
            public_id="publicflags02",
            course=None,
        )

        response = self.client.get(
            "/datapipeline/api/get_feedback_gpt_by_public_id/",
            {"public_id": survey.public_id},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["completion_certificate_enabled"])
        self.assertFalse(payload["parsed_document_download_enabled"])


class CompletionCertificateMigrationTest(TransactionTestCase):
    migrate_from = [("datapipeline", "0041_add_cmpm80k_pdf_ingest_profile")]
    migrate_to = [("datapipeline", "0042_course_completion_downloads")]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.old_apps = self.executor.loader.project_state(self.migrate_from).apps
        OldCourse = self.old_apps.get_model("datapipeline", "Course")
        OldCourse.objects.create(
            course_id="legacy-course",
            course_name="Legacy Course",
            instructor_name="Prof. Legacy",
            password="pw",
        )

        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        self.apps = self.executor.loader.project_state(self.migrate_to).apps

    def test_forward_migration_enables_existing_courses_only(self):
        Course = self.apps.get_model("datapipeline", "Course")

        legacy = Course.objects.get(course_id="legacy-course")
        created_after = Course.objects.create(
            course_id="new-course",
            course_name="New Course",
            instructor_name="Prof. New",
            password="pw",
        )

        self.assertTrue(legacy.parsed_document_download_enabled)
        self.assertFalse(created_after.parsed_document_download_enabled)

    def test_forward_migration_0042_does_not_add_display_snapshot_yet(self):
        SurveyCompletionCertificate = self.apps.get_model(
            "datapipeline",
            "SurveyCompletionCertificate",
        )
        field_names = {
            field.name for field in SurveyCompletionCertificate._meta.local_fields
        }

        self.assertNotIn("display_snapshot", field_names)

    def test_reverse_migration_removes_0042_only_state(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        reversed_apps = self.executor.loader.project_state(self.migrate_from).apps
        Course = reversed_apps.get_model("datapipeline", "Course")

        legacy = Course.objects.get(course_id="legacy-course")
        field_names = {field.name for field in Course._meta.local_fields}

        self.assertEqual(legacy.course_name, "Legacy Course")
        self.assertNotIn("parsed_document_download_enabled", field_names)
        self.assertNotIn("completion_certificate_enabled", field_names)
        self.assertNotIn("display_snapshot", field_names)
        self.assertNotIn(
            "datapipeline_surveycompletioncertificate",
            connection.introspection.table_names(),
        )


class CompletionCertificateDisplaySnapshotMigrationTest(TransactionTestCase):
    migrate_from = [("datapipeline", "0042_course_completion_downloads")]
    migrate_to = [("datapipeline", "0043_certificate_display_snapshot")]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        self.apps = self.executor.loader.project_state(self.migrate_to).apps

    def test_forward_migration_0043_adds_display_snapshot_field_with_default_dict(self):
        SurveyCompletionCertificate = self.apps.get_model(
            "datapipeline",
            "SurveyCompletionCertificate",
        )
        field_names = {
            field.name for field in SurveyCompletionCertificate._meta.local_fields
        }

        self.assertIn("display_snapshot", field_names)
        field = SurveyCompletionCertificate._meta.get_field("display_snapshot")
        self.assertEqual(field.get_default(), {})


class CompletionServiceTest(TestCase):
    def setUp(self):
        self.course = _make_course(course_name="CMPM 101")
        self.survey = _make_survey(self.course)

    def test_generated_code_is_80_bit_human_readable(self):
        code = generate_code()

        self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){3}$")
        self.assertNotRegex(code, r"[IO01]")
        self.assertEqual(len(normalize_code(code).replace("-", "")), 16)

    def test_normalize_code_rejects_ambiguous_or_malformed_values(self):
        self.assertEqual(
            normalize_code("abcd efgh jklm npqr"),
            "ABCD-EFGH-JKLM-NPQR",
        )

        for raw in ("ABCD-EFGH-IJKL-MNPQ", "ABCD-EFGH-JKLM-NPQ", "ABCD-EFGH-JKLM-NPQ0"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    normalize_code(raw)

    def test_snapshot_drops_private_fields_and_clamps_allowed_values(self):
        clean = sanitize_progress_snapshot(
            {
                "version": 99,
                "mode": "group",
                "student_message_count": 999,
                "complete": True,
                "schema_id": "s" * 120,
                "area_index": 999,
                "area_id": "a" * 120,
                "area_title": "Collaboration" * 30,
                "engine_turn": 99999,
                "last_directive_kind": "directive" * 20,
                "response_text": "private answer",
                "question_title": "Secret question",
                "section_title": "Secret section",
                "email": "student@example.edu",
                "student_id": "A12345",
                "session_id": "private-session",
            },
            mode="form",
            student_message_count=3,
        )

        self.assertEqual(clean["version"], 1)
        self.assertEqual(clean["mode"], "form")
        self.assertEqual(clean["student_message_count"], 3)
        self.assertTrue(clean["complete"])
        self.assertEqual(clean["schema_id"], "s" * 100)
        self.assertEqual(clean["area_index"], 500)
        self.assertEqual(clean["area_id"], "a" * 100)
        self.assertEqual(clean["area_title"], ("Collaboration" * 30)[:200])
        self.assertEqual(clean["engine_turn"], 10000)
        self.assertEqual(clean["last_directive_kind"], ("directive" * 20)[:100])
        self.assertNotIn("response_text", clean)
        self.assertNotIn("question_title", clean)
        self.assertNotIn("section_title", clean)
        self.assertNotIn("email", clean)
        self.assertNotIn("student_id", clean)
        self.assertNotIn("session_id", clean)

    def test_snapshot_rejects_non_string_text_field_values(self):
        clean = sanitize_progress_snapshot(
            {
                "complete": True,
                "schema_id": {"bad": "value"},
                "area_id": ["not", "a", "string"],
                "area_title": 123,
                "last_directive_kind": ("tuple",),
            },
            mode="form",
            student_message_count=4,
        )

        self.assertEqual(clean["student_message_count"], 4)
        self.assertTrue(clean["complete"])
        self.assertNotIn("schema_id", clean)
        self.assertNotIn("area_id", clean)
        self.assertNotIn("area_title", clean)
        self.assertNotIn("last_directive_kind", clean)

    def test_snapshot_rejects_non_bool_complete_and_non_int_progress_fields(self):
        clean = sanitize_progress_snapshot(
            {
                "complete": "false",
                "area_index": "7",
                "engine_turn": True,
            },
            mode="form",
            student_message_count=5,
        )

        self.assertEqual(clean["mode"], "form")
        self.assertEqual(clean["student_message_count"], 5)
        self.assertFalse(clean["complete"])
        self.assertNotIn("area_index", clean)
        self.assertNotIn("engine_turn", clean)

    def test_snapshot_invalid_mode_falls_back_to_general_without_stringifying(self):
        clean = sanitize_progress_snapshot(
            {
                "complete": 1,
                "schema_id": "schema-kept-only-in-form",
            },
            mode={"not": "a-string"},
            student_message_count=6,
        )

        self.assertEqual(clean["mode"], "general")
        self.assertEqual(clean["student_message_count"], 6)
        self.assertFalse(clean["complete"])
        self.assertNotIn("schema_id", clean)

    def test_eligible_student_message_count_uses_actual_roles_and_excludes_pdf_rows(self):
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user",
            content="First student turn",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="Second student turn",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="student",
            content="Imported chat-style student turn",
            gpt_used="test",
            gpt_id=self.survey.pk,
            source=FeedbackMessage.SOURCE_CHAT,
        )
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="student",
            content="PDF reflection should not count",
            gpt_used="test",
            gpt_id=self.survey.pk,
            source=FeedbackMessage.SOURCE_PDF,
        )
        for role in ("assistant", "ai", "ai-message"):
            FeedbackMessage.objects.create(
                session_id="session-1",
                student_id="anon",
                sent_by=role,
                content=f"{role} turn",
                gpt_used="test",
                gpt_id=self.survey.pk,
            )
        other_survey = _make_survey(self.course, public_id="cert-other")
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="Other survey turn",
            gpt_used="test",
            gpt_id=other_survey.pk,
        )

        self.assertEqual(
            eligible_student_message_count(self.survey.pk, "session-1"),
            3,
        )

    def test_issue_or_get_certificate_is_stable_and_reuses_first_snapshot(self):
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="Finished reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )

        with mock.patch(
            "datapipeline.leai_completion.generate_code",
            return_value="ABCD-EFGH-JKLM-NPQR",
        ) as generate:
            first = issue_or_get_certificate(
                self.survey,
                "session-1",
                {"complete": True, "response_text": "private answer"},
            )
            second = issue_or_get_certificate(
                self.survey,
                "session-1",
                {"complete": False, "response_text": "new answer"},
            )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.code, "ABCD-EFGH-JKLM-NPQR")
        self.assertEqual(first.progress_snapshot["student_message_count"], 1)
        self.assertTrue(first.progress_snapshot["complete"])
        self.assertNotIn("response_text", first.progress_snapshot)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(
            SurveyCompletionCertificate.objects.filter(
                survey=self.survey,
                session_id="session-1",
            ).count(),
            1,
        )

    def test_issue_or_get_certificate_retries_on_code_collision_for_distinct_sessions(self):
        SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="session-1",
            code="ABCD-EFGH-JKLM-NPQR",
            progress_snapshot={"version": 1},
        )
        for session_id in ("session-1", "session-2"):
            FeedbackMessage.objects.create(
                session_id=session_id,
                student_id="anon",
                sent_by="user-message",
                content=f"Finished reflection for {session_id}",
                gpt_used="test",
                gpt_id=self.survey.pk,
            )

        with mock.patch(
            "datapipeline.leai_completion.generate_code",
            side_effect=["ABCD-EFGH-JKLM-NPQR", "RSTU-VWXY-Z234-5678"],
        ) as generate:
            certificate = issue_or_get_certificate(
                self.survey,
                "session-2",
                {"complete": True},
            )

        self.assertEqual(certificate.code, "RSTU-VWXY-Z234-5678")
        self.assertEqual(generate.call_count, 2)

    def test_issue_or_get_certificate_includes_generic_submission_instruction_when_legacy_flag_false(self):
        self.assertFalse(self.survey.canvas_integration)
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="Finished reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )

        certificate = issue_or_get_certificate(
            self.survey,
            "session-1",
            {"complete": True},
        )
        text = _pdf_text(render_certificate_pdf(certificate))

        self.assertEqual(
            certificate.display_snapshot,
            {
                "course_name": "CMPM 101",
                "survey_name": "Week 1 Reflection",
                "week_label": "Week 1",
                "canvas_guidance": "Submit as instructed by your instructor.",
            },
        )
        self.assertIn("Submit as instructed by your instructor.", text)

    def test_issue_or_get_certificate_backfills_empty_display_snapshot_and_freezes_pdf(self):
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="Finished reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )
        certificate = SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="session-1",
            code="ABCD-EFGH-JKLM-NPQR",
            progress_snapshot={"version": 1, "complete": True},
            display_snapshot={},
        )

        issued = issue_or_get_certificate(self.survey, "session-1", {"complete": False})
        first_pdf = render_certificate_pdf(issued)

        self.course.course_name = "Renamed Course"
        self.course.save(update_fields=["course_name"])
        self.survey.name = "Renamed Survey"
        self.survey.survey_label = "Week 9"
        self.survey.week_number = 9
        self.survey.save(
            update_fields=["name", "survey_label", "week_number"]
        )

        issued_again = issue_or_get_certificate(self.survey, "session-1", {"complete": False})
        second_pdf = render_certificate_pdf(issued_again)
        certificate.refresh_from_db()

        self.assertEqual(issued.pk, certificate.pk)
        self.assertEqual(issued_again.pk, certificate.pk)
        self.assertEqual(
            certificate.display_snapshot,
            {
                "course_name": "CMPM 101",
                "survey_name": "Week 1 Reflection",
                "week_label": "Week 1",
                "canvas_guidance": "Submit as instructed by your instructor.",
            },
        )
        self.assertEqual(first_pdf, second_pdf)
        self.assertIn("CMPM 101", _pdf_text(second_pdf))
        self.assertNotIn("Renamed Course", _pdf_text(second_pdf))

    def test_existing_frozen_snapshot_without_canvas_guidance_uses_current_submission_instruction(self):
        self.survey.canvas_integration = True
        self.survey.save(update_fields=["canvas_integration"])
        certificate = SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="session-1",
            code="ABCD-EFGH-JKLM-NPQR",
            progress_snapshot={"version": 1, "complete": True},
            display_snapshot={
                "course_name": "CMPM 101",
                "survey_name": "Week 1 Reflection",
                "week_label": "Week 1",
            },
        )

        issued = issue_or_get_certificate(self.survey, "session-1", {"complete": False})
        text = _pdf_text(render_certificate_pdf(issued))
        certificate.refresh_from_db()

        self.assertEqual(issued.pk, certificate.pk)
        self.assertEqual(
            certificate.display_snapshot,
            {
                "course_name": "CMPM 101",
                "survey_name": "Week 1 Reflection",
                "week_label": "Week 1",
            },
        )
        self.assertIn("Submit as instructed by your instructor.", text)

    def test_render_certificate_pdf_contains_required_fields_without_private_data(self):
        certificate = SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="private-session-42",
            code="ABCD-EFGH-JKLM-NPQR",
            progress_snapshot={
                "version": 1,
                "mode": "form",
                "student_message_count": 3,
                "complete": True,
                "response_text": "private answer",
                "question_title": "Question 1",
                "area_title": "Private Area Title",
                "email": "student@example.edu",
                "student_id": "A12345",
            },
        )

        self.assertEqual(getattr(certificate, "display_snapshot", None), {})
        pdf_bytes = render_certificate_pdf(certificate)
        text = _pdf_text(pdf_bytes)

        self.assertIn("GUII Lab Completion Certificate", text)
        self.assertIn("CMPM 101", text)
        self.assertIn("Week 1 Reflection", text)
        self.assertIn("Week 1", text)
        self.assertIn("ABCD-EFGH-JKLM-NPQR", text)
        self.assertIn("Verification required", text)
        self.assertIn(
            "A visual copy alone is not proof of issuance",
            text.replace("\n", " "),
        )
        self.assertIn("Submit as instructed by your instructor.", text)
        self.assertNotIn("Submit via Canvas.", text)

    def test_render_certificate_pdf_replaces_legacy_canvas_guidance_with_current_instruction(self):
        certificate = SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="session-1",
            code="ABCD-EFGH-JKLM-NPQR",
            progress_snapshot={"version": 1, "complete": True},
            display_snapshot={
                "course_name": "CMPM 101",
                "survey_name": "Week 1 Reflection",
                "week_label": "Week 1",
                "canvas_guidance": "Submit via Canvas.",
            },
        )

        text = _pdf_text(render_certificate_pdf(certificate))

        self.assertIn("Submit as instructed by your instructor.", text)
        self.assertNotIn("Submit via Canvas.", text)
        self.assertIn(
            "after at least one response was saved",
            text.replace("\n", " "),
        )
        self.assertNotIn("confirms that the survey was completed", text)
        self.assertNotIn("private-session-42", text)
        self.assertNotIn("private answer", text)
        self.assertNotIn("Question 1", text)
        self.assertNotIn("Private Area Title", text)
        self.assertNotIn("student@example.edu", text)
        self.assertNotIn("A12345", text)


class CertificateIssuanceApiTest(TestCase):
    url = "/datapipeline/api/issue_completion_certificate/"

    def setUp(self):
        self.client = Client()
        self.course = _make_course(course_name="CMPM 101")
        self.course.completion_certificate_enabled = True
        self.course.save(update_fields=["completion_certificate_enabled"])
        self.survey = _make_survey(self.course)

    def _issue(self, payload, *, raw=False):
        if raw:
            return self.client.post(
                self.url,
                data=payload,
                content_type="application/json",
            )
        return _post(self.client, self.url, payload)

    def test_issue_completion_certificate_rejects_non_post_requests(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)

    def test_issue_completion_certificate_rejects_invalid_json(self):
        response = self._issue("{bad json", raw=True)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Invalid JSON"})

    def test_issue_completion_certificate_rejects_non_object_json_bodies_without_private_content(self):
        client = Client(raise_request_exception=False)

        for payload in ([], "private-session-1", None):
            with self.subTest(payload=payload):
                response = client.post(
                    self.url,
                    data=json.dumps(payload),
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertEqual(
                    response.json(),
                    {"error": "Invalid request body"},
                )
                body = response.content.decode("utf-8")
                self.assertNotIn("AttributeError", body)
                self.assertNotIn("Traceback", body)
                self.assertNotIn("private-session-1", body)

    def test_issue_completion_certificate_validates_required_string_bounds(self):
        public_id_max = FeedbackGPT._meta.get_field("public_id").max_length
        session_id_max = SurveyCompletionCertificate._meta.get_field("session_id").max_length

        cases = (
            {"public_id": "", "session_id": "session-1"},
            {"public_id": self.survey.public_id, "session_id": ""},
            {"public_id": "x" * (public_id_max + 1), "session_id": "session-1"},
            {"public_id": self.survey.public_id, "session_id": "x" * (session_id_max + 1)},
            {"public_id": 17, "session_id": "session-1"},
            {"public_id": self.survey.public_id, "session_id": 17},
        )

        for payload in cases:
            with self.subTest(payload=payload):
                response = self._issue(payload)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json(),
                    {"error": "public_id and session_id are required"},
                )

    def test_issue_completion_certificate_returns_404_for_unknown_survey(self):
        response = self._issue(
            {"public_id": "missing-survey", "session_id": "session-1"}
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Survey not found"})

    def test_issue_completion_certificate_returns_403_when_disabled(self):
        self.course.completion_certificate_enabled = False
        self.course.save(update_fields=["completion_certificate_enabled"])
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user",
            content="Finished reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )

        response = self._issue(
            {"public_id": self.survey.public_id, "session_id": "session-1"}
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"error": "Completion certificates are not enabled for this course"},
        )

    def test_issue_completion_certificate_returns_409_for_empty_session(self):
        response = self._issue(
            {"public_id": self.survey.public_id, "session_id": "session-1"}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"error": "No persisted student response found for this session"},
        )

    def test_issue_completion_certificate_returns_409_for_assistant_only_session(self):
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="assistant",
            content="Assistant-only turn",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )

        response = self._issue(
            {"public_id": self.survey.public_id, "session_id": "session-1"}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"error": "No persisted student response found for this session"},
        )

    def test_issue_completion_certificate_returns_409_for_pdf_only_session(self):
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="student",
            content="PDF-only reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
            source=FeedbackMessage.SOURCE_PDF,
        )

        response = self._issue(
            {"public_id": self.survey.public_id, "session_id": "session-1"}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"error": "No persisted student response found for this session"},
        )

    def test_issue_completion_certificate_accepts_legacy_student_roles_and_returns_stable_private_pdf(self):
        for idx, role in enumerate(("user", "user-message", "student"), start=1):
            FeedbackMessage.objects.create(
                session_id="session-1",
                student_id="anon",
                sent_by=role,
                content=f"Student turn {idx}",
                gpt_used="test",
                gpt_id=self.survey.pk,
            )

        first = self._issue(
            {
                "public_id": self.survey.public_id,
                "session_id": "session-1",
                "progress_snapshot": {
                    "complete": True,
                    "response_text": "private answer",
                    "student_id": "A12345",
                },
            }
        )
        second = self._issue(
            {
                "public_id": self.survey.public_id,
                "session_id": "session-1",
                "progress_snapshot": {"complete": False},
            }
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first["Content-Type"], "application/pdf")
        self.assertEqual(second["Content-Type"], "application/pdf")
        self.assertEqual(first["Cache-Control"], "no-store, private")
        self.assertEqual(second["Cache-Control"], "no-store, private")
        self.assertEqual(first.content, second.content)

        certificate = SurveyCompletionCertificate.objects.get(
            survey=self.survey,
            session_id="session-1",
        )
        text = _pdf_text(first.content)

        self.assertIn(certificate.code, text)
        self.assertIn("CMPM 101", text)
        self.assertIn("Week 1 Reflection", text)
        self.assertNotIn("private answer", text)
        self.assertNotIn("A12345", text)
        self.assertNotIn("session-1", text)
        self.assertNotIn(self.survey.public_id, text)
        self.assertNotIn(str(certificate.pk), text)
        self.assertTrue(certificate.progress_snapshot["complete"])
        self.assertEqual(certificate.progress_snapshot["student_message_count"], 3)
        self.assertEqual(
            certificate.display_snapshot,
            {
                "course_name": "CMPM 101",
                "survey_name": "Week 1 Reflection",
                "week_label": "Week 1",
                "canvas_guidance": "Submit as instructed by your instructor.",
            },
        )

    def test_issue_completion_certificate_freezes_display_snapshot_after_metadata_changes(self):
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="Finished reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )

        first = self._issue(
            {
                "public_id": self.survey.public_id,
                "session_id": "session-1",
                "progress_snapshot": {
                    "complete": True,
                    "response_text": "private answer",
                },
            }
        )
        certificate = SurveyCompletionCertificate.objects.get(
            survey=self.survey,
            session_id="session-1",
        )
        first_code = certificate.code

        self.course.course_name = "Renamed Course"
        self.course.save(update_fields=["course_name"])
        self.survey.name = "Renamed Survey"
        self.survey.survey_label = "Week 9"
        self.survey.week_number = 9
        self.survey.save(
            update_fields=["name", "survey_label", "week_number"]
        )

        second = self._issue(
            {
                "public_id": self.survey.public_id,
                "session_id": "session-1",
                "progress_snapshot": {"complete": False},
            }
        )

        certificate.refresh_from_db()
        display_snapshot = getattr(certificate, "display_snapshot", None)
        second_text = _pdf_text(second.content)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.content, second.content)
        self.assertEqual(certificate.code, first_code)
        self.assertEqual(
            display_snapshot,
            {
                "course_name": "CMPM 101",
                "survey_name": "Week 1 Reflection",
                "week_label": "Week 1",
                "canvas_guidance": "Submit as instructed by your instructor.",
            },
        )
        self.assertEqual(set(display_snapshot.keys()), {
            "course_name",
            "survey_name",
            "week_label",
            "canvas_guidance",
        })
        self.assertNotIn("response_text", certificate.progress_snapshot)
        self.assertNotIn("course_name", certificate.progress_snapshot)
        self.assertNotIn("survey_name", certificate.progress_snapshot)
        self.assertNotIn("week_label", certificate.progress_snapshot)
        self.assertNotIn("canvas_guidance", certificate.progress_snapshot)
        self.assertNotIn("session_id", display_snapshot)
        self.assertNotIn("student_message_count", display_snapshot)
        self.assertIn("CMPM 101", second_text)
        self.assertIn("Week 1 Reflection", second_text)
        self.assertIn("Week 1", second_text)
        self.assertIn("Submit as instructed by your instructor.", second_text)
        self.assertNotIn("Renamed Course", second_text)
        self.assertNotIn("Renamed Survey", second_text)
        self.assertNotIn("Week 9", second_text)

    def test_issue_completion_certificate_sanitizes_headers_and_hostile_pdf_labels(self):
        hostile_course = _make_course(
            course_id="hostile-course",
            course_name="CMPM 101\r\nSet-Cookie:\x0b private",
        )
        hostile_course.completion_certificate_enabled = True
        hostile_course.save(update_fields=["completion_certificate_enabled"])
        hostile_survey = _make_survey(hostile_course, public_id="hostile-public")
        hostile_survey.name = "Week 1 Reflection\r\nX-Ignore: true"
        hostile_survey.survey_label = "Week\t1\x0cLabel"
        hostile_survey.save(update_fields=["name", "survey_label"])
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="Finished reflection",
            gpt_used="test",
            gpt_id=hostile_survey.pk,
        )

        response = self._issue(
            {
                "public_id": hostile_survey.public_id,
                "session_id": "session-1",
                "progress_snapshot": {"response_text": "private answer"},
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertNotIn("\r", response["Content-Disposition"])
        self.assertNotIn("\n", response["Content-Disposition"])
        self.assertNotIn("session-1", response["Content-Disposition"])
        self.assertNotIn(hostile_survey.public_id, response["Content-Disposition"])

        text = _pdf_text(response.content)

        self.assertIn("CMPM 101 Set-Cookie: private", text)
        self.assertIn("Week 1 Reflection X-Ignore: true", text)
        self.assertIn("Week 1 Label", text)
        self.assertNotIn("private answer", text)
        self.assertNotIn("session-1", text)
        self.assertNotIn(hostile_survey.public_id, text)

    def test_issue_completion_certificate_returns_generic_500_without_private_ids(self):
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user",
            content="Finished reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )

        with mock.patch(
            "datapipeline.views.issue_or_get_certificate",
            side_effect=RuntimeError(
                f"boom {self.survey.public_id} session-1 certificate-uuid"
            ),
        ):
            response = self._issue(
                {"public_id": self.survey.public_id, "session_id": "session-1"}
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {"error": "Unable to issue completion certificate"},
        )
        self.assertNotIn(self.survey.public_id, response.content.decode("utf-8"))
        self.assertNotIn("session-1", response.content.decode("utf-8"))
        self.assertNotIn("certificate-uuid", response.content.decode("utf-8"))


class CompletionCertificateConcurrencyTest(TransactionTestCase):
    def setUp(self):
        self.course = _make_course(course_id="concurrency-course")
        self.survey = _make_survey(self.course, public_id="concurrency1")

    def test_issue_or_get_certificate_is_stable_under_same_session_concurrency(self):
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="Finished reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def synced_code():
            barrier.wait(timeout=5)
            return "ABCD-EFGH-JKLM-NPQR"

        def issue():
            connections.close_all()
            try:
                certificate = issue_or_get_certificate(
                    FeedbackGPT.objects.get(pk=self.survey.pk),
                    "session-1",
                    {"complete": True},
                )
                results.append((certificate.pk, certificate.code))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                connections.close_all()

        with mock.patch("datapipeline.leai_completion.generate_code", side_effect=synced_code):
            threads = [threading.Thread(target=issue) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            SurveyCompletionCertificate.objects.filter(
                survey=self.survey,
                session_id="session-1",
            ).count(),
            1,
        )

class CompletionCertificateVerificationApiTest(TestCase):
    url = "/datapipeline/api/verify_completion_certificates/"

    def setUp(self):
        self.client = Client()
        self.course = _make_course(
            course_id="verify-course",
            course_name="Verification Course",
        )
        self.survey = _make_survey(self.course, public_id="verify-public")
        self.other_survey = _make_survey(self.course, public_id="verify-public-2")
        self.other_course = _make_course(
            course_id="foreign-course",
            course_name="Foreign Course",
        )
        self.foreign_survey = _make_survey(
            self.other_course,
            public_id="verify-public-3",
        )
        self.primary_certificate = SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="session-1",
            code="ABCD-EFGH-JKLM-NPQR",
            progress_snapshot={"version": 1, "complete": True},
            display_snapshot={"course_name": "Verification Course"},
        )
        self.secondary_certificate = SurveyCompletionCertificate.objects.create(
            survey=self.survey,
            session_id="session-2",
            code="STUV-WXYZ-2345-6789",
            progress_snapshot={"version": 1, "complete": True},
            display_snapshot={"course_name": "Verification Course"},
        )
        self.other_survey_certificate = SurveyCompletionCertificate.objects.create(
            survey=self.other_survey,
            session_id="session-3",
            code="BCDF-GHJK-LMNP-QRST",
            progress_snapshot={"version": 1, "complete": True},
            display_snapshot={"course_name": "Verification Course"},
        )
        self.foreign_certificate = SurveyCompletionCertificate.objects.create(
            survey=self.foreign_survey,
            session_id="session-4",
            code="VWXY-Z234-5678-9ABC",
            progress_snapshot={"version": 1, "complete": True},
            display_snapshot={"course_name": "Foreign Course"},
        )

    def _verify(self, payload, *, raw=False):
        if raw:
            return self.client.post(
                self.url,
                data=payload,
                content_type="application/json",
            )
        return _post(self.client, self.url, payload)

    def test_verify_completion_certificates_rejects_non_post_requests(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)

    def test_verify_completion_certificates_rejects_invalid_json(self):
        response = self._verify("{bad json", raw=True)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertEqual(response.json(), {"error": "Invalid JSON"})

    def test_verify_completion_certificates_rejects_non_object_json_bodies_without_private_content(self):
        client = Client(raise_request_exception=False)

        for payload in ([], "private-session-1", None):
            with self.subTest(payload=payload):
                response = client.post(
                    self.url,
                    data=json.dumps(payload),
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response["Cache-Control"], "no-store, private")
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertEqual(response.json(), {"error": "Invalid request body"})
                body = response.content.decode("utf-8")
                self.assertNotIn("AttributeError", body)
                self.assertNotIn("Traceback", body)
                self.assertNotIn("private-session-1", body)

    def test_verify_completion_certificates_validates_required_payload_shape(self):
        cases = (
            {"course_id": "", "survey_id": self.survey.id, "codes": []},
            {"course_id": self.course.course_id, "survey_id": "7", "codes": []},
            {"course_id": self.course.course_id, "survey_id": self.survey.id, "codes": "ABCD-EFGH-JKLM-NPQR"},
            {"course_id": self.course.course_id, "survey_id": self.survey.id, "codes": ["ABCD-EFGH-JKLM-NPQR", 17]},
        )

        for payload in cases:
            with self.subTest(payload=payload):
                response = self._verify(payload)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response["Cache-Control"], "no-store, private")
                self.assertEqual(
                    response.json(),
                    {"error": "course_id, survey_id, and codes are required"},
                )

    def test_verify_completion_certificates_rejects_batches_over_100(self):
        response = self._verify({
            "course_id": self.course.course_id,
            "survey_id": self.survey.id,
            "codes": [self.primary_certificate.code] * 101,
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertEqual(
            response.json(),
            {"error": "codes must contain at most 100 entries"},
        )

    def test_verify_completion_certificates_returns_ordered_results_without_mutation(self):
        payload = {
            "course_id": self.course.course_id,
            "survey_id": self.survey.id,
            "codes": [
                "abcd efgh jklm npqr",
                "stuvwxyz23456789",
                self.other_survey_certificate.code,
                "QRST UVWX YZ23 4567",
                "too-long-code-too-long-code",
                "oops",
                self.primary_certificate.code,
            ],
        }
        before = _certificate_rows_snapshot()

        response = self._verify(payload)
        repeated = self._verify(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), repeated.json())

        results = response.json()["results"]
        self.assertEqual(len(results), len(payload["codes"]))
        self.assertEqual(
            [result["status"] for result in results],
            [
                "valid",
                "valid",
                "not_found",
                "not_found",
                "not_found",
                "not_found",
                "valid",
            ],
        )
        self.assertEqual(results[0]["code"], self.primary_certificate.code)
        self.assertEqual(results[1]["code"], self.secondary_certificate.code)
        self.assertEqual(results[2]["code"], self.other_survey_certificate.code)
        self.assertEqual(results[3]["code"], "QRST-UVWX-YZ23-4567")
        self.assertEqual(results[6]["code"], self.primary_certificate.code)
        for result in results:
            self.assertEqual(set(result.keys()), {"code", "status"})

        self.assertEqual(_certificate_rows_snapshot(), before)

    def test_verify_completion_certificates_requires_exact_survey_within_course(self):
        payload = {
            "course_id": self.course.course_id,
            "survey_id": self.foreign_survey.id,
            "codes": [self.primary_certificate.code, self.foreign_certificate.code],
        }
        before = _certificate_rows_snapshot()

        response = self._verify(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertEqual(
            response.json(),
            {
                "results": [
                    {"code": self.primary_certificate.code, "status": "not_found"},
                    {"code": self.foreign_certificate.code, "status": "not_found"},
                ]
            },
        )
        self.assertEqual(_certificate_rows_snapshot(), before)


class SurveyResponseExportRegressionTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.course = _make_course(course_id="export-course")
        self.survey = _make_survey(self.course, public_id="export-public")
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="First reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )

    def test_export_survey_responses_omits_completion_code_column(self):
        response = self.client.get(
            "/datapipeline/api/export_survey_responses/",
            {"survey_id": self.survey.id},
        )

        self.assertEqual(response.status_code, 200)
        header = response.content.decode().splitlines()[0]
        self.assertEqual(header, "session_id,sent_by,content,created_at")
        self.assertNotIn("completion_code", response.content.decode())


class CanvasIntegrationRetirementApiTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.course = _make_course(course_id="canvas-course")
        self.survey = _make_survey(self.course, public_id="canvas-public")

    def test_create_feedback_gpt_ignores_canvas_integration_input(self):
        response = _post(
            self.client,
            "/datapipeline/api/create_feedback_gpt/",
            {
                "course_id": self.course.course_id,
                "name": "Canvas Survey",
                "instructions": "Be helpful.",
                "canvas_integration": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        created = FeedbackGPT.objects.get(pk=response.json()["id"])
        self.assertFalse(created.canvas_integration)

    def test_get_feedback_gpt_by_public_id_omits_canvas_integration(self):
        self.survey.canvas_integration = True
        self.survey.save(update_fields=["canvas_integration"])

        response = self.client.get(
            "/datapipeline/api/get_feedback_gpt_by_public_id/",
            {"public_id": self.survey.public_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("canvas_integration", response.json())

    def test_feedback_messages_by_course_omits_canvas_integration(self):
        self.survey.canvas_integration = True
        self.survey.save(update_fields=["canvas_integration"])
        FeedbackMessage.objects.create(
            session_id="session-1",
            student_id="anon",
            sent_by="user-message",
            content="First reflection",
            gpt_used="test",
            gpt_id=self.survey.pk,
        )

        response = self.client.get(
            "/datapipeline/api/feedback_messages_by_course/",
            {"course_id": self.course.course_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("canvas_integration", response.json()[0])

    def test_update_survey_ignores_canvas_integration_input(self):
        self.survey.canvas_integration = True
        self.survey.save(update_fields=["canvas_integration"])

        response = _post(
            self.client,
            "/datapipeline/api/update_survey/",
            {
                "survey_id": self.survey.id,
                "name": "Renamed Survey",
                "canvas_integration": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.name, "Renamed Survey")
        self.assertTrue(self.survey.canvas_integration)
        self.assertNotIn("canvas_integration", response.json())
