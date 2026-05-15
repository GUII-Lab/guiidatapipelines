"""Tests for the LEAI PDF reflection ingest pipeline.

Covers parser, mapper, worker, view layer (start/poll/commit/revert),
dedup, idempotent revert, and source visibility on the existing list
endpoint. Uses an inline-thread shim so the async worker runs on the
request thread (mirrors the chat-turn test pattern).
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse

from datapipeline import leai_pdf_ingest
from datapipeline.models import (
    Course,
    FeedbackGPT,
    FeedbackMessage,
    FormSchema,
    LEAIPdfIngestBatch,
    LEAIPdfIngestJob,
)


# Test fixtures ------------------------------------------------------------

DEFAULT_SECTIONS = [
    {"id": "1.1", "title": "Key Concepts", "opening_prompt": "What concept?"},
    {"id": "1.2", "title": "Methods in Practice", "opening_prompt": "What method?"},
    {"id": "1.3", "title": "Open Question Reflection", "opening_prompt": "What's open?"},
]


def make_pdf(lines: list[str]) -> bytes:
    """Render a simple text-only PDF for parser tests (reportlab dev dep)."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 11)
    y = 750
    for line in lines:
        c.drawString(72, y, line)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


def make_clean_pdf() -> bytes:
    return make_pdf([
        "HCI 271 Weekly Reflection",
        "Name: Jane Doe",
        "",
        "Key Concepts",
        "This week the most important concept was contextual inquiry.",
        "It is the practice of going to where users work.",
        "",
        "Methods in Practice",
        "I used affinity diagramming with my team.",
        "We grouped sticky notes by theme.",
        "",
        "Open Question Reflection",
        "I am still uncertain how to scale this to large datasets.",
    ])


def make_partial_pdf() -> bytes:
    """Missing the second section."""
    return make_pdf([
        "Key Concepts",
        "Stuff about contextual inquiry.",
        "",
        "Open Question Reflection",
        "Some open questions.",
    ])


# Inline-thread shim -------------------------------------------------------

class _InlineThread:
    def __init__(self, target=None, args=(), name=None, daemon=None, **kwargs):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def inline_thread_patch():
    return patch("datapipeline.leai_pdf_ingest.threading.Thread", _InlineThread)


# ─── Parser / mapper ─────────────────────────────────────────────────────

class ParserTests(TestCase):
    def test_extract_text_from_real_pdf(self):
        text = leai_pdf_ingest._extract_pdf_text(make_clean_pdf())
        self.assertIn("Key Concepts", text)
        self.assertIn("Methods in Practice", text)
        self.assertIn("Open Question Reflection", text)

    def test_empty_blob_raises(self):
        with self.assertRaisesRegex(ValueError, "Empty"):
            leai_pdf_ingest._extract_pdf_text(b"")

    def test_garbage_blob_raises(self):
        with self.assertRaises(ValueError):
            leai_pdf_ingest._extract_pdf_text(b"not a pdf")

    def test_flatten_prompts_handles_missing_body(self):
        self.assertEqual(leai_pdf_ingest.flatten_prompts_from_schema(None), [])
        self.assertEqual(leai_pdf_ingest.flatten_prompts_from_schema({}), [])
        self.assertEqual(leai_pdf_ingest.flatten_prompts_from_schema({"sections": "x"}), [])

    def test_clean_mapping(self):
        prompts = leai_pdf_ingest.flatten_prompts_from_schema({"sections": DEFAULT_SECTIONS})
        text = leai_pdf_ingest._extract_pdf_text(make_clean_pdf())
        mapping, low, _ = leai_pdf_ingest.map_text_to_prompts(text, prompts)
        self.assertEqual(low, [])
        self.assertIn("contextual inquiry", mapping["1.1"])
        self.assertIn("affinity diagramming", mapping["1.2"])
        self.assertIn("scale this to large datasets", mapping["1.3"])

    def test_missing_section_marked_low_conf(self):
        prompts = leai_pdf_ingest.flatten_prompts_from_schema({"sections": DEFAULT_SECTIONS})
        text = leai_pdf_ingest._extract_pdf_text(make_partial_pdf())
        mapping, low, _ = leai_pdf_ingest.map_text_to_prompts(text, prompts)
        self.assertIn("1.2", low)
        self.assertEqual(mapping["1.2"], "")
        self.assertNotEqual(mapping["1.1"], "")
        self.assertNotEqual(mapping["1.3"], "")

    def test_no_headings_at_all(self):
        prompts = leai_pdf_ingest.flatten_prompts_from_schema({"sections": DEFAULT_SECTIONS})
        text = "Just some unstructured prose with no section headings."
        mapping, low, preamble = leai_pdf_ingest.map_text_to_prompts(text, prompts)
        # Every prompt is low-confidence and the preamble contains the text.
        self.assertEqual(set(low), {"1.1", "1.2", "1.3"})
        self.assertIn("unstructured prose", preamble)


# ─── Worker ──────────────────────────────────────────────────────────────

class WorkerTests(TestCase):
    def setUp(self):
        self.course = Course.objects.create(
            course_id="t-course", course_name="T", instructor_name="i", password="p",
        )
        self.schema = FormSchema.objects.create(
            schema_id="t-schema", title="T", body={"sections": DEFAULT_SECTIONS},
        )
        self.survey = FeedbackGPT.objects.create(
            name="Wk 6 Reflection", instructions="x", public_id="wk6-reflect",
            course=self.course, mode="form", form_schema=self.schema,
        )

    def test_worker_processes_each_file(self):
        files = [("alice.pdf", make_clean_pdf()), ("bob.pdf", make_partial_pdf())]
        attribs = {"alice.pdf": "alice", "bob.pdf": "bob"}
        with inline_thread_patch():
            job = leai_pdf_ingest.start_pdf_ingest_job(
                self.survey, files, attribs, created_by="instructor",
            )
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")
        self.assertEqual(len(job.items), 2)
        alice = next(it for it in job.items if it["filename"] == "alice.pdf")
        bob = next(it for it in job.items if it["filename"] == "bob.pdf")
        self.assertEqual(alice["status"], "ok")
        self.assertEqual(alice["student_id"], "alice")
        self.assertIn("contextual inquiry", alice["mapping"]["1.1"])
        self.assertEqual(bob["status"], "low_conf")
        self.assertIn("1.2", bob["low_conf_prompts"])

    def test_worker_records_per_file_failure_without_killing_batch(self):
        files = [
            ("good.pdf", make_clean_pdf()),
            ("bad.pdf", b"not a pdf at all"),
        ]
        attribs = {"good.pdf": "alice", "bad.pdf": "bob"}
        with inline_thread_patch():
            job = leai_pdf_ingest.start_pdf_ingest_job(self.survey, files, attribs)
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")
        statuses = {it["filename"]: it["status"] for it in job.items}
        self.assertEqual(statuses["good.pdf"], "ok")
        self.assertEqual(statuses["bad.pdf"], "failed")

    def test_missing_attribution_is_caller_error(self):
        with self.assertRaisesRegex(ValueError, "Missing student"):
            leai_pdf_ingest.start_pdf_ingest_job(
                self.survey, [("a.pdf", make_clean_pdf())], {},
            )

    def test_oversize_file_rejected_before_worker(self):
        big = b"x" * (leai_pdf_ingest.MAX_BYTES_PER_FILE + 1)
        with self.assertRaisesRegex(ValueError, "10 MB"):
            leai_pdf_ingest.start_pdf_ingest_job(
                self.survey, [("a.pdf", big)], {"a.pdf": "alice"},
            )

    def test_parallel_active_job_rejected_with_existing(self):
        # Seed an existing pending job so a fresh start triggers the
        # conflict guard. We don't run a worker for the seed — the row
        # itself is enough to claim the survey's "active" slot.
        from django.utils import timezone
        active = LEAIPdfIngestJob.objects.create(
            survey=self.survey,
            status=LEAIPdfIngestJob.STATUS_RUNNING,
            job_started_at=timezone.now(),
        )
        with self.assertRaises(leai_pdf_ingest.IngestJobConflict) as cm:
            leai_pdf_ingest.start_pdf_ingest_job(
                self.survey,
                [("a.pdf", make_clean_pdf())],
                {"a.pdf": "alice"},
            )
        self.assertEqual(cm.exception.existing_job.pk, active.pk)
        # No new job was created.
        self.assertEqual(LEAIPdfIngestJob.objects.filter(survey=self.survey).count(), 1)

    def test_worker_survives_schema_with_no_prompts(self):
        # Defensive: a misconfigured survey whose FormSchema body has no
        # sections should not crash the worker. Every prompt becomes
        # low_conf-ish (no mapping possible), and the item flag is 'ok'
        # because there are no low-confidence prompts to flag — just
        # nothing to extract. The frontend handles the empty-prompts
        # case via the schemaEmpty banner before it gets here.
        empty_schema = FormSchema.objects.create(
            schema_id="empty-schema", title="Empty", body={"sections": []},
        )
        empty_survey = FeedbackGPT.objects.create(
            name="Empty Survey", instructions="x", public_id="empty-1",
            course=self.course, mode="form", form_schema=empty_schema,
        )
        with inline_thread_patch():
            job = leai_pdf_ingest.start_pdf_ingest_job(
                empty_survey,
                [("alice.pdf", make_clean_pdf())],
                {"alice.pdf": "alice"},
            )
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")
        self.assertEqual(len(job.items), 1)
        item = job.items[0]
        self.assertEqual(item["status"], "ok")  # nothing to flag low_conf
        self.assertEqual(item["mapping"], {})  # nothing to map

    def test_ai_assist_fills_low_conf_prompts(self):
        # When regex misses a section (template heading drift), the
        # second-pass AI call should be invoked and its result merged
        # into the mapping. The affected prompt should move out of
        # low_conf_prompts and into ai_assisted_prompts.
        from unittest.mock import patch

        # Use a PDF that has 1.1 and 1.2 (per DEFAULT_SECTIONS used in
        # this test class's setUp) but is missing the 1.3 'Open Question
        # Reflection' section — regex will flag 1.3 as low-conf, and the
        # mocked AI will fill it in.
        partial_pdf_lines = [
            "Key Concepts",
            "This week the most important concept was contextual inquiry.",
            "",
            "Methods in Practice",
            "I used affinity diagramming.",
        ]
        from io import BytesIO
        from reportlab.pdfgen import canvas as _c
        from reportlab.lib.pagesizes import letter as _l
        buf = BytesIO()
        c = _c.Canvas(buf, pagesize=_l)
        c.setFont("Helvetica", 11)
        y = 750
        for line in partial_pdf_lines:
            c.drawString(72, y, line); y -= 16
        c.showPage(); c.save()
        partial = buf.getvalue()

        ai_mock_response = {
            "parsed": {"1.3": "AI-extracted Open Question answer."},
            "response": "{}",
            "usage": {},
        }
        with patch("datapipeline.leai_pdf_ingest.openai_client.run_structured",
                   return_value=ai_mock_response), \
             inline_thread_patch():
            job = leai_pdf_ingest.start_pdf_ingest_job(
                self.survey,
                [("alice.pdf", partial)],
                {"alice.pdf": "alice"},
            )
        job.refresh_from_db()
        item = job.items[0]
        # 1.3 should now be filled by AI assist; not flagged low_conf.
        self.assertEqual(item["mapping"].get("1.3"),
                         "AI-extracted Open Question answer.")
        self.assertNotIn("1.3", item["low_conf_prompts"])
        self.assertIn("1.3", item.get("ai_assisted_prompts", []))
        # Item status reflects the upgraded outcome — no remaining low-conf.
        self.assertEqual(item["status"], "ok")

    def test_ai_assist_failure_leaves_regex_mapping_intact(self):
        # When the AI call raises, we should fall back to the regex
        # result without crashing the worker. The prompts the AI would
        # have filled remain low_conf so the human sees them.
        from unittest.mock import patch
        from io import BytesIO
        from reportlab.pdfgen import canvas as _c
        from reportlab.lib.pagesizes import letter as _l
        buf = BytesIO()
        c = _c.Canvas(buf, pagesize=_l)
        c.setFont("Helvetica", 11)
        y = 750
        for line in ["Key Concepts & Takeaways", "Just one section."]:
            c.drawString(72, y, line); y -= 16
        c.showPage(); c.save()
        partial = buf.getvalue()

        with patch("datapipeline.leai_pdf_ingest.openai_client.run_structured",
                   side_effect=RuntimeError("simulated AI outage")), \
             inline_thread_patch():
            job = leai_pdf_ingest.start_pdf_ingest_job(
                self.survey,
                [("alice.pdf", partial)],
                {"alice.pdf": "alice"},
            )
        job.refresh_from_db()
        item = job.items[0]
        # Worker stays alive, outcome reflects regex-only result.
        self.assertEqual(job.status, "ready")
        # Should remain low-conf — many prompts unmatched and AI failed.
        self.assertIn(item["status"], ("low_conf", "ok"))
        self.assertEqual(item.get("ai_assisted_prompts", []), [])

    def test_stale_active_job_does_not_block_new_one(self):
        # Stale jobs (past PDF_INGEST_JOB_STALE_SECONDS) should not
        # block — they're treated as dead and superseded.
        from datetime import timedelta
        from django.utils import timezone
        stale_when = timezone.now() - timedelta(
            seconds=leai_pdf_ingest.PDF_INGEST_JOB_STALE_SECONDS + 60,
        )
        LEAIPdfIngestJob.objects.create(
            survey=self.survey,
            status=LEAIPdfIngestJob.STATUS_RUNNING,
            job_started_at=stale_when,
        )
        with inline_thread_patch():
            job = leai_pdf_ingest.start_pdf_ingest_job(
                self.survey,
                [("alice.pdf", make_clean_pdf())],
                {"alice.pdf": "alice"},
            )
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")


# ─── Commit + revert ─────────────────────────────────────────────────────

class CommitRevertTests(TestCase):
    def setUp(self):
        self.course = Course.objects.create(
            course_id="c", course_name="C", instructor_name="i", password="p",
        )
        self.schema = FormSchema.objects.create(
            schema_id="s", title="S", body={"sections": DEFAULT_SECTIONS},
        )
        self.survey = FeedbackGPT.objects.create(
            name="S1", instructions="x", public_id="commit-1",
            course=self.course, mode="form", form_schema=self.schema,
        )

    def _ready_job(self) -> LEAIPdfIngestJob:
        with inline_thread_patch():
            return leai_pdf_ingest.start_pdf_ingest_job(
                self.survey,
                [("alice.pdf", make_clean_pdf())],
                {"alice.pdf": "alice"},
            )

    def test_commit_creates_messages_and_batch(self):
        job = self._ready_job()
        job.refresh_from_db()
        item = job.items[0]
        batch = leai_pdf_ingest.commit_pdf_ingest_job(
            job, [{
                "filename": item["filename"],
                "student_id": item["student_id"],
                "mapping": item["mapping"],
                "skip": False,
            }], dedup_decisions={}, committed_by="instructor",
        )
        self.assertEqual(batch.student_count, 1)
        self.assertEqual(batch.message_count, 3)  # 3 sections
        msgs = FeedbackMessage.objects.filter(pdf_batch=batch)
        self.assertEqual(msgs.count(), 3)
        self.assertTrue(all(m.source == "pdf" for m in msgs))
        # Job is consumed.
        self.assertFalse(LEAIPdfIngestJob.objects.filter(pk=job.pk).exists())

    def test_dedup_replace_overwrites_existing(self):
        # Pre-existing PDF row for this student/survey
        FeedbackMessage.objects.create(
            session_id="seed", student_id="alice", sent_by="student",
            content="OLD", gpt_used=self.survey.name, gpt_id=self.survey.id,
            source="pdf",
        )
        job = self._ready_job()
        job.refresh_from_db()
        item = job.items[0]
        leai_pdf_ingest.commit_pdf_ingest_job(
            job, [{"filename": item["filename"], "student_id": "alice",
                   "mapping": item["mapping"], "skip": False}],
            dedup_decisions={"alice": "replace"},
        )
        contents = list(FeedbackMessage.objects.filter(student_id="alice").values_list("content", flat=True))
        self.assertNotIn("OLD", contents)
        self.assertEqual(len(contents), 3)

    def test_dedup_skip_keeps_existing_no_new(self):
        FeedbackMessage.objects.create(
            session_id="seed", student_id="alice", sent_by="student",
            content="OLD", gpt_used=self.survey.name, gpt_id=self.survey.id,
            source="pdf",
        )
        job = self._ready_job()
        job.refresh_from_db()
        item = job.items[0]
        batch = leai_pdf_ingest.commit_pdf_ingest_job(
            job, [{"filename": item["filename"], "student_id": "alice",
                   "mapping": item["mapping"], "skip": False}],
            dedup_decisions={"alice": "skip"},
        )
        contents = list(FeedbackMessage.objects.filter(student_id="alice").values_list("content", flat=True))
        self.assertEqual(contents, ["OLD"])
        # Batch row exists but with zero committed messages.
        self.assertEqual(batch.message_count, 0)

    def test_revert_deletes_only_batch_messages(self):
        # Seed an unrelated chat message that must survive.
        chat_msg = FeedbackMessage.objects.create(
            session_id="chat-1", student_id="bob", sent_by="student",
            content="from chat", gpt_used=self.survey.name, gpt_id=self.survey.id,
            source="chat",
        )
        job = self._ready_job()
        job.refresh_from_db()
        item = job.items[0]
        batch = leai_pdf_ingest.commit_pdf_ingest_job(
            job, [{"filename": item["filename"], "student_id": "alice",
                   "mapping": item["mapping"], "skip": False}],
            dedup_decisions={},
        )
        self.assertEqual(FeedbackMessage.objects.filter(pdf_batch=batch).count(), 3)
        deleted = leai_pdf_ingest.revert_pdf_ingest_batch(batch)
        self.assertEqual(deleted, 3)
        batch.refresh_from_db()
        self.assertIsNotNone(batch.reverted_at)
        # Chat row survives.
        self.assertTrue(FeedbackMessage.objects.filter(pk=chat_msg.pk).exists())
        # Idempotent — second revert is a no-op.
        self.assertEqual(leai_pdf_ingest.revert_pdf_ingest_batch(batch), 0)


# ─── HTTP layer ──────────────────────────────────────────────────────────

class ViewLayerTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.course = Course.objects.create(
            course_id="hci", course_name="HCI", instructor_name="i", password="p",
        )
        self.schema = FormSchema.objects.create(
            schema_id="hci-s", title="S", body={"sections": DEFAULT_SECTIONS},
        )
        self.survey = FeedbackGPT.objects.create(
            name="Wk 6", instructions="x", public_id="wk6-view",
            course=self.course, mode="form", form_schema=self.schema,
        )

    def _start(self, files: list[tuple[str, bytes]], attribs: dict[str, str]):
        upload = [
            ("files", (fname, blob, "application/pdf")) for fname, blob in files
        ]
        # Django test client expects upload files via SimpleUploadedFile or tuple-list.
        from django.core.files.uploadedfile import SimpleUploadedFile
        files_param = [SimpleUploadedFile(fname, blob, content_type="application/pdf")
                       for fname, blob in files]
        with inline_thread_patch():
            return self.client.post(
                reverse("leai_pdf_ingest_start"),
                data={
                    "survey_id": self.survey.id,
                    "attributions": json.dumps(attribs),
                    "files": files_param,
                },
            )

    def test_start_then_poll_then_commit_then_revert(self):
        resp = self._start([("alice.pdf", make_clean_pdf())], {"alice.pdf": "alice"})
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        job_id = body["job_id"]
        self.assertEqual(body["status"], "ready")  # inline thread already ran
        self.assertEqual(len(body["items"]), 1)
        self.assertGreater(len(body["prompts"]), 0)

        # Poll
        poll = self.client.get(reverse("leai_pdf_ingest_detail", args=[job_id]))
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.json()["status"], "ready")

        # Commit
        item = body["items"][0]
        commit = self.client.post(
            reverse("leai_pdf_ingest_commit", args=[job_id]),
            data=json.dumps({
                "items": [{"filename": item["filename"], "student_id": "alice",
                           "mapping": item["mapping"], "skip": False}],
                "dedup_decisions": {},
                "committed_by": "instructor",
            }),
            content_type="application/json",
        )
        self.assertEqual(commit.status_code, 200, commit.content)
        commit_body = commit.json()
        self.assertEqual(commit_body["committed_count"], 3)
        batch_id = commit_body["batch"]["batch_id"]

        # source visible on list endpoint
        listing = self.client.get(
            reverse("feedback_messages_by_gpt") + f"?gpt_id={self.survey.id}"
        )
        sessions = listing.json()["sessions"]
        all_msgs = [m for msgs in sessions.values() for m in msgs]
        self.assertTrue(all(m["source"] == "pdf" for m in all_msgs))
        self.assertTrue(all(m["pdf_batch_id"] == batch_id for m in all_msgs))

        # batches list
        batches = self.client.get(reverse("leai_pdf_ingest_batches_list") + f"?survey_id={self.survey.id}")
        self.assertEqual(len(batches.json()), 1)

        # revert
        revert = self.client.post(reverse("leai_pdf_ingest_batch_revert", args=[batch_id]))
        self.assertEqual(revert.status_code, 200)
        self.assertEqual(revert.json()["deleted_count"], 3)
        self.assertEqual(FeedbackMessage.objects.filter(gpt_id=self.survey.id).count(), 0)

        # second revert → 409
        again = self.client.post(reverse("leai_pdf_ingest_batch_revert", args=[batch_id]))
        self.assertEqual(again.status_code, 409)

    def test_start_returns_409_when_job_already_active(self):
        # Seed an active job so a fresh /start/ for the same survey
        # collides — endpoint should return 409 with existing_job
        # so the frontend can resume polling instead of starting over.
        from django.utils import timezone
        from django.core.files.uploadedfile import SimpleUploadedFile
        seed = LEAIPdfIngestJob.objects.create(
            survey=self.survey,
            status=LEAIPdfIngestJob.STATUS_RUNNING,
            job_started_at=timezone.now(),
        )
        f = SimpleUploadedFile("a.pdf", make_clean_pdf(), content_type="application/pdf")
        resp = self.client.post(
            reverse("leai_pdf_ingest_start"),
            data={
                "survey_id": self.survey.id,
                "attributions": json.dumps({"a.pdf": "alice"}),
                "files": [f],
            },
        )
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertIn("existing_job", body)
        self.assertEqual(body["existing_job"]["job_id"], str(seed.pk))

    def test_start_rejects_non_form_survey(self):
        general = FeedbackGPT.objects.create(
            name="general", instructions="x", public_id="general-1",
            course=self.course, mode="general",
        )
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("a.pdf", make_clean_pdf(), content_type="application/pdf")
        resp = self.client.post(
            reverse("leai_pdf_ingest_start"),
            data={"survey_id": general.id, "attributions": json.dumps({"a.pdf": "a"}),
                  "files": [f]},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Structured Reflection", resp.json()["error"])

    def test_dedup_check_endpoint(self):
        FeedbackMessage.objects.create(
            session_id="x", student_id="alice", sent_by="student",
            content="prev", gpt_used=self.survey.name, gpt_id=self.survey.id,
            source="pdf",
        )
        resp = self.client.post(
            reverse("leai_pdf_ingest_dedup_check"),
            data=json.dumps({"survey_id": self.survey.id, "student_ids": ["alice", "bob"]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["existing"], ["alice"])

    def test_commit_rejects_malformed_items(self):
        # Seed a ready job so commit reaches the validator.
        with inline_thread_patch():
            job = leai_pdf_ingest.start_pdf_ingest_job(
                self.survey,
                [("a.pdf", make_clean_pdf())],
                {"a.pdf": "alice"},
            )
        url = reverse("leai_pdf_ingest_commit", args=[str(job.pk)])
        # items[0].mapping is a string (should be a dict) — clear 400.
        resp = self.client.post(
            url,
            data=json.dumps({
                "items": [{"filename": "a.pdf", "student_id": "alice", "mapping": "oops"}],
                "dedup_decisions": {},
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("mapping", resp.json()["error"])

        # Bad dedup choice → clear 400.
        resp = self.client.post(
            url,
            data=json.dumps({
                "items": [{"filename": "a.pdf", "student_id": "alice", "mapping": {}}],
                "dedup_decisions": {"alice": "ZAP"},
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("dedup_decisions", resp.json()["error"])

    def test_roster_endpoint_combines_chat_and_pdf(self):
        FeedbackMessage.objects.create(
            session_id="s1", student_id="alice", sent_by="student",
            content="chat", gpt_used=self.survey.name, gpt_id=self.survey.id,
            source="chat",
        )
        FeedbackMessage.objects.create(
            session_id="s2", student_id="bob", sent_by="student",
            content="pdf", gpt_used=self.survey.name, gpt_id=self.survey.id,
            source="pdf",
        )
        resp = self.client.get(reverse("leai_pdf_ingest_roster") + f"?survey_id={self.survey.id}")
        self.assertEqual(resp.status_code, 200)
        ids = {s["student_id"]: s for s in resp.json()["students"]}
        self.assertEqual(set(ids), {"alice", "bob"})
        self.assertTrue(ids["alice"]["submitted_to_this_survey"])
        self.assertFalse(ids["alice"]["has_pdf_on_this_survey"])
        self.assertTrue(ids["bob"]["has_pdf_on_this_survey"])
