"""LEAI PDF reflection ingest.

Parses instructor-uploaded PDFs that follow the same sectioned-reflection
template as the bound FormSchema, maps each section to the matching
schema prompt, and (after instructor confirmation) writes the answers as
FeedbackMessage rows so they appear alongside chat-collected responses.

Worker pattern mirrors `leai_analysis.start_quicktake_job` and
`start_chat_turn_job`: a `threading.Thread(daemon=True)` populates a
status row, the frontend polls. Stale rows past
`PDF_INGEST_JOB_STALE_SECONDS` are auto-failed by the GET endpoint.

Public surface
--------------
- ``start_pdf_ingest_job(survey, files, attributions, created_by)``
- ``commit_pdf_ingest_job(job, items, dedup_decisions, committed_by)``
- ``revert_pdf_ingest_batch(batch)``
- ``flatten_prompts_from_schema(body)``  (also used by view layer for hints)
- ``is_job_stale(job)``
"""

from __future__ import annotations

import io
import logging
import re
import threading
import unicodedata
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from .models import (
    FeedbackGPT,
    FeedbackMessage,
    LEAIPdfIngestBatch,
    LEAIPdfIngestJob,
    LEAIQuickTake,
)

logger = logging.getLogger(__name__)

# Stale recovery: a job in pending/running past this many seconds is
# auto-failed by the GET endpoint. 30 minutes covers the worst case of
# a 50-PDF batch on a slow dyno.
PDF_INGEST_JOB_STALE_SECONDS = 1800

# Hard limit per ingest call. Enforced server-side too even though the
# frontend caps at the same number, since the worker holds files in
# memory the whole run.
MAX_FILES_PER_BATCH = 60
MAX_BYTES_PER_FILE = 10 * 1024 * 1024  # 10 MB
MAX_BYTES_PER_BATCH = 50 * 1024 * 1024  # 50 MB

# Above this length, an extracted answer block is treated as suspicious
# (likely we matched the wrong heading and swept multiple sections).
ANSWER_BLOCK_SOFT_MAX_CHARS = 8000

# Regex helpers ------------------------------------------------------------

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_LEADING_NUM_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*[\.\)]?\s+|[A-Z]\d+[\.\)]?\s+|Q\d+[\.\)]?\s+|Section\s+[A-Z0-9]+[:\.\)]?\s+|Part\s+[A-Z0-9]+[:\.\)]?\s+)?")


def _normalise_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _CONTROL_CHARS_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


# PDF extraction -----------------------------------------------------------

def _extract_pdf_text(blob: bytes) -> str:
    """Extract concatenated text from a PDF blob.

    Raises ``ValueError`` with a human-friendly message on parse failure
    (encrypted, corrupted, image-only). The view layer surfaces the
    message back to the instructor.
    """
    if not blob:
        raise ValueError("Empty file.")
    try:
        # pypdf is pure-Python, no system deps. Required in requirements.txt.
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as e:  # pragma: no cover — deployment misconfig
        raise ValueError(f"PDF library unavailable: {e}") from e

    try:
        reader = PdfReader(io.BytesIO(blob))
    except PdfReadError as e:
        raise ValueError(f"Couldn't read this PDF ({e}).") from e
    except Exception as e:
        raise ValueError(f"Couldn't read this PDF ({type(e).__name__}).") from e

    if reader.is_encrypted:
        # Try empty-password unlock; password-protected PDFs are rejected.
        try:
            reader.decrypt("")
        except Exception:
            raise ValueError("This PDF is password-protected.")

    pages_text: list[str] = []
    for page in reader.pages:
        try:
            pages_text.append(page.extract_text() or "")
        except Exception as e:  # pragma: no cover — pypdf occasional page-level glitch
            logger.warning("page extract failed: %s", e)
            pages_text.append("")

    text = _normalise_text("\n\n".join(pages_text))
    if not text:
        raise ValueError("No extractable text — is this a scanned image PDF?")
    return text


# Schema flattening --------------------------------------------------------

def flatten_prompts_from_schema(body: dict | None) -> list[dict]:
    """Reduce a FormSchema.body to a flat list of prompt descriptors.

    Returns
    -------
    list of {prompt_id, title, topic, opening_prompt}
        - prompt_id: section id (e.g. '1.1') — what we use as the mapping key
        - title: human-readable heading we try to find in the PDF
        - topic: short description, used as a heading-match fallback
        - opening_prompt: full prompt text, used to render the review UI

    Defensive: handles missing/null body, missing fields, non-list sections.
    """
    if not isinstance(body, dict):
        return []
    sections = body.get("sections")
    if not isinstance(sections, list):
        return []
    out: list[dict] = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        pid = str(sec.get("id") or "").strip()
        title = str(sec.get("title") or "").strip()
        topic = str(sec.get("topic") or "").strip()
        opening = str(sec.get("opening_prompt") or "").strip()
        if not pid:
            continue
        out.append({
            "prompt_id": pid,
            "title": title,
            "topic": topic,
            "opening_prompt": opening,
        })
    return out


# Section mapping ----------------------------------------------------------

def _build_heading_regex(title: str, prompt_id: str) -> re.Pattern[str] | None:
    """Build a forgiving regex for finding a section heading in PDF text.

    Matches the title (case-insensitive, whitespace-flexible), allowing
    optional leading numbering, Markdown-style markers, or trailing
    colons. Also accepts the bare ``prompt_id`` (e.g. ``1.1``) on its own
    line as a fallback for templates that just number sections.
    """
    title = (title or "").strip()
    prompt_id = (prompt_id or "").strip()
    if not title and not prompt_id:
        return None

    # Title → list of escaped tokens, rejoined with \s+ in the *pattern*
    # (not via re.sub on the escaped string, which mishandles \s in the
    # replacement and escapes spaces in Python 3.11+).
    title_pattern = ""
    if title:
        tokens = [re.escape(tok) for tok in title.split() if tok]
        if tokens:
            title_pattern = r"\s+".join(tokens)

    pid_pattern = re.escape(prompt_id) if prompt_id else ""

    parts: list[str] = []
    if title_pattern:
        prefix_opt = (
            rf"(?:{pid_pattern}[\s\.:\)]+)?" if pid_pattern else ""
        )
        # Heading line: optional start-of-line whitespace/markers,
        # optional id prefix, the title, optional trailing punctuation,
        # then end-of-line.
        parts.append(
            r"(?:^|\n)[\s>#*\-]*"
            rf"{prefix_opt}"
            rf"{title_pattern}"
            r"\s*[:\.\-—]?\s*(?=\n|$)"
        )
    if pid_pattern:
        # Bare-id fallback: e.g. "1.1" alone on a line.
        parts.append(rf"(?:^|\n)\s*{pid_pattern}[\s\.:\)][^\n]*(?=\n|$)")
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE | re.MULTILINE)


def _find_first_match(pattern: re.Pattern[str], text: str, start: int = 0) -> tuple[int, int] | None:
    m = pattern.search(text, start)
    if not m:
        return None
    return m.start(), m.end()


def map_text_to_prompts(
    text: str,
    prompts: list[dict],
) -> tuple[dict[str, str], list[str], str]:
    """Heuristic section-mapping: text → {prompt_id: answer_text}.

    Walks the text once looking for each prompt's heading in *schema
    order*. The span between match `i` and match `i+1` becomes the
    answer block for prompt `i`. Prompts with no heading match end up
    in `low_conf_prompts` with an empty answer.

    Returns
    -------
    (mapping, low_conf_prompt_ids, unmatched_leading_text)
        - mapping: {prompt_id: answer_text} for every prompt (empty string
          if no match found)
        - low_conf_prompt_ids: prompts where match was missing or block
          looks suspicious (empty / too long / unmatched-bucket-only)
        - unmatched_leading_text: text before the first matched heading
          (cover page / instructions / etc.). Surfaced in the review UI
          under the "Unmatched preamble" affordance.
    """
    if not prompts:
        return {}, [], text

    text = _normalise_text(text)
    matches: list[tuple[str, int, int]] = []  # (prompt_id, start, end)

    cursor = 0
    for p in prompts:
        pat = _build_heading_regex(p.get("title", ""), p.get("prompt_id", ""))
        if not pat:
            continue
        span = _find_first_match(pat, text, cursor)
        if span is not None:
            matches.append((p["prompt_id"], span[0], span[1]))
            cursor = span[1]

    mapping: dict[str, str] = {p["prompt_id"]: "" for p in prompts}
    low_conf: list[str] = []

    if not matches:
        # No headings found at all — every prompt is low-confidence and
        # the entire text becomes the unmatched preamble.
        return mapping, [p["prompt_id"] for p in prompts], text

    # Preamble = text before the first matched heading's start position.
    preamble = text[: matches[0][1]].strip()

    for i, (pid, _start, end) in enumerate(matches):
        next_start = matches[i + 1][1] if i + 1 < len(matches) else len(text)
        block = text[end:next_start].strip()
        # Soft length cap — if a single section block is huge it usually
        # means we missed a heading and swept multiple sections.
        if len(block) > ANSWER_BLOCK_SOFT_MAX_CHARS:
            low_conf.append(pid)
        if not block:
            low_conf.append(pid)
        mapping[pid] = block

    matched_ids = {pid for (pid, _s, _e) in matches}
    for p in prompts:
        if p["prompt_id"] not in matched_ids and p["prompt_id"] not in low_conf:
            low_conf.append(p["prompt_id"])

    return mapping, low_conf, preamble


# Stale recovery -----------------------------------------------------------

def is_job_stale(job: LEAIPdfIngestJob) -> bool:
    """True if the job is pending/running past the stale window."""
    if job.status not in (LEAIPdfIngestJob.STATUS_PENDING, LEAIPdfIngestJob.STATUS_RUNNING):
        return False
    started = job.job_started_at or job.created_at
    if not started:
        return False
    return (timezone.now() - started).total_seconds() > PDF_INGEST_JOB_STALE_SECONDS


# Worker -------------------------------------------------------------------

def start_pdf_ingest_job(
    survey: FeedbackGPT,
    files: list[tuple[str, bytes]],
    attributions: dict[str, str],
    created_by: str = "",
) -> LEAIPdfIngestJob:
    """Create a job row and spawn the background worker.

    Parameters
    ----------
    survey:
        Must be in ``mode='form'`` and have a bound FormSchema. Caller
        validates this; we trust it here.
    files:
        ``[(filename, blob_bytes), ...]``. Validated for size by caller.
    attributions:
        ``{filename: student_id}``. Every filename must have an entry.

    Returns
    -------
    The created LEAIPdfIngestJob row, with status='pending'.
    """
    if not files:
        raise ValueError("At least one file is required.")
    if len(files) > MAX_FILES_PER_BATCH:
        raise ValueError(f"Too many files (max {MAX_FILES_PER_BATCH} per batch).")
    total_bytes = sum(len(b) for _, b in files)
    if total_bytes > MAX_BYTES_PER_BATCH:
        raise ValueError("Batch exceeds 50 MB total.")
    for fname, blob in files:
        if len(blob) > MAX_BYTES_PER_FILE:
            raise ValueError(f"{fname} exceeds 10 MB.")
        if fname not in attributions or not attributions[fname]:
            raise ValueError(f"Missing student attribution for {fname}.")

    job = LEAIPdfIngestJob.objects.create(
        survey=survey,
        created_by=created_by or "",
        progress={"processed": 0, "total": len(files)},
    )

    # Snapshot survey bits the worker needs so it doesn't refetch under
    # a different DB connection (Django + threads + per-conn ORM caches).
    schema_body = survey.form_schema.body if survey.form_schema_id else None

    def _worker(job_pk, files_in_memory, attribution_map, schema_body):
        try:
            LEAIPdfIngestJob.objects.filter(pk=job_pk).update(
                status=LEAIPdfIngestJob.STATUS_RUNNING,
                job_started_at=timezone.now(),
            )
            prompts = flatten_prompts_from_schema(schema_body)
            items: list[dict] = []
            for idx, (filename, blob) in enumerate(files_in_memory):
                student_id = attribution_map.get(filename, "")
                item: dict[str, Any] = {
                    "filename": filename,
                    "student_id": student_id,
                    "status": "ok",
                    "extracted_text": "",
                    "mapping": {},
                    "low_conf_prompts": [],
                    "preamble": "",
                    "error": "",
                }
                try:
                    text = _extract_pdf_text(blob)
                    mapping, low_conf, preamble = map_text_to_prompts(text, prompts)
                    item["extracted_text"] = text
                    item["mapping"] = mapping
                    item["low_conf_prompts"] = low_conf
                    item["preamble"] = preamble
                    item["status"] = "low_conf" if low_conf else "ok"
                except Exception as e:
                    item["status"] = "failed"
                    item["error"] = str(e)
                    logger.info("pdf ingest item failed for %s: %s", filename, e)
                items.append(item)
                LEAIPdfIngestJob.objects.filter(pk=job_pk).update(
                    items=items,
                    progress={"processed": idx + 1, "total": len(files_in_memory)},
                )
            LEAIPdfIngestJob.objects.filter(pk=job_pk).update(
                status=LEAIPdfIngestJob.STATUS_READY,
            )
        except Exception as e:
            logger.exception("pdf_ingest worker crashed for job=%s", job_pk)
            LEAIPdfIngestJob.objects.filter(pk=job_pk).update(
                status=LEAIPdfIngestJob.STATUS_FAILED,
                error=f"Internal error: {type(e).__name__}",
            )
        finally:
            # Only close the DB connection if we're actually on a worker
            # thread; inline-thread test shims run on the main thread and
            # closing here would orphan the request's connection.
            if threading.current_thread() is not threading.main_thread():
                connection.close()

    thread = threading.Thread(
        target=_worker,
        name=f"pdf-ingest-{job.pk}",
        args=(job.pk, list(files), dict(attributions), schema_body),
        daemon=True,
    )
    thread.start()
    return job


# Commit -------------------------------------------------------------------

def commit_pdf_ingest_job(
    job: LEAIPdfIngestJob,
    confirmed_items: list[dict],
    dedup_decisions: dict[str, str],
    committed_by: str = "",
) -> LEAIPdfIngestBatch:
    """Persist the confirmed mapping as FeedbackMessage rows.

    Parameters
    ----------
    job:
        The ingest job (must be status='ready'; preview was generated).
    confirmed_items:
        Frontend-edited list, one entry per file the instructor wants
        committed:
            {filename, student_id, mapping: {prompt_id: text}, skip: bool}
        Items with ``skip=True`` are recorded in ``items_summary`` but
        produce no FeedbackMessage rows.
    dedup_decisions:
        ``{student_id: 'replace'|'skip'|'add'}``. Default 'add' if a
        student isn't listed but already has PDF rows for this survey.

    Returns
    -------
    The created LEAIPdfIngestBatch.
    """
    if job.status != LEAIPdfIngestJob.STATUS_READY:
        raise ValueError("Job is not ready to commit.")
    survey = job.survey
    schema_body = survey.form_schema.body if survey.form_schema_id else None
    prompts = flatten_prompts_from_schema(schema_body)
    prompt_titles = {p["prompt_id"]: p.get("title") or p["prompt_id"] for p in prompts}

    items_summary: list[dict] = []
    student_ids_committed: set[str] = set()
    messages_to_create: list[FeedbackMessage] = []
    student_replace_targets: set[str] = set()

    # Resolve dedup decisions ahead of any writes.
    for item in confirmed_items:
        sid = (item.get("student_id") or "").strip()
        if not sid or item.get("skip"):
            continue
        decision = dedup_decisions.get(sid, "add")
        if decision == "replace":
            student_replace_targets.add(sid)

    with transaction.atomic():
        # Per-student replace: delete existing PDF rows for this survey
        # before inserting new ones. We do this in a single bulk delete
        # per student to keep it atomic.
        for sid in student_replace_targets:
            FeedbackMessage.objects.filter(
                gpt_id=survey.id, student_id=sid, source=FeedbackMessage.SOURCE_PDF,
            ).delete()

        batch = LEAIPdfIngestBatch.objects.create(
            survey=survey,
            committed_by=committed_by or "",
        )

        for item in confirmed_items:
            filename = item.get("filename") or ""
            sid = (item.get("student_id") or "").strip()
            mapping = item.get("mapping") or {}
            skip = bool(item.get("skip"))
            decision = dedup_decisions.get(sid, "add") if sid else "add"

            if not sid:
                items_summary.append({
                    "filename": filename, "student_id": "",
                    "status": "skipped", "reason": "no_student",
                    "prompt_count": 0,
                })
                continue
            if skip or decision == "skip":
                items_summary.append({
                    "filename": filename, "student_id": sid,
                    "status": "skipped",
                    "reason": "instructor_skip" if skip else "dedup_skip",
                    "prompt_count": 0,
                })
                continue

            session_id = f"pdf-{batch.id}-{_safe_session_slug(filename)}"
            prompt_count = 0
            for prompt_id, answer in mapping.items():
                answer = (answer or "").strip()
                if not answer:
                    continue
                title = prompt_titles.get(prompt_id, prompt_id)
                content = f"Q: {title}\n\nA: {answer}"
                messages_to_create.append(FeedbackMessage(
                    session_id=session_id,
                    student_id=sid,
                    sent_by="student",
                    content=content,
                    gpt_used=survey.name,
                    gpt_id=survey.id,
                    research_consent=False,
                    source=FeedbackMessage.SOURCE_PDF,
                    pdf_batch=batch,
                ))
                prompt_count += 1

            student_ids_committed.add(sid)
            items_summary.append({
                "filename": filename, "student_id": sid,
                "status": "committed",
                "dedup": decision,
                "prompt_count": prompt_count,
            })

        if messages_to_create:
            FeedbackMessage.objects.bulk_create(messages_to_create, batch_size=500)

        batch.student_count = len(student_ids_committed)
        batch.message_count = len(messages_to_create)
        batch.items_summary = items_summary
        batch.save(update_fields=["student_count", "message_count", "items_summary"])

        # Invalidate Quick Take rows for this course so the next view
        # regenerates with the new corpus included.
        if survey.course_id:
            LEAIQuickTake.objects.filter(course_id=survey.course_id).delete()

        # Done with the preview — drop the transient job row.
        LEAIPdfIngestJob.objects.filter(pk=job.pk).delete()

    return batch


def revert_pdf_ingest_batch(batch: LEAIPdfIngestBatch) -> int:
    """Hard-delete the FeedbackMessage rows the batch created.

    Idempotent for the row-delete itself; the second call will see
    `reverted_at` already set and return 0 without touching anything.
    """
    if batch.reverted_at:
        return 0
    with transaction.atomic():
        deleted_count, _ = FeedbackMessage.objects.filter(pdf_batch=batch).delete()
        batch.reverted_at = timezone.now()
        batch.save(update_fields=["reverted_at"])
        if batch.survey and batch.survey.course_id:
            LEAIQuickTake.objects.filter(course_id=batch.survey.course_id).delete()
    return deleted_count


# Helpers ------------------------------------------------------------------

_SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _safe_session_slug(filename: str) -> str:
    """Slug a filename for use in synthetic session_id values."""
    base = (filename or "").rsplit(".", 1)[0].lower()
    slug = _SAFE_SLUG_RE.sub("-", base).strip("-")
    return slug[:60] or "pdf"


def detect_existing_pdf_students(survey: FeedbackGPT, student_ids: list[str]) -> list[str]:
    """Return the subset of student_ids that already have PDF responses
    for this survey. Used to drive dedup decisions in the commit modal.
    """
    if not student_ids:
        return []
    return list(
        FeedbackMessage.objects
        .filter(
            gpt_id=survey.id,
            student_id__in=student_ids,
            source=FeedbackMessage.SOURCE_PDF,
        )
        .values_list("student_id", flat=True)
        .distinct()
    )
