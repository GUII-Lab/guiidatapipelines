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
from . import openai_client

logger = logging.getLogger(__name__)

# AI-assist model: a small fast model is plenty for "match these blocks
# of text to these prompts". Cost is ~$0.001 per PDF with low-conf
# prompts, only fires when regex misses.
AI_ASSIST_MODEL = "gpt-4o-mini"
# Cap the extracted text we send to OpenAI to avoid blowing the context
# window or runaway cost on a giant PDF — 12 KB covers ~3 pages of dense
# text, which is far beyond a typical reflection.
AI_ASSIST_TEXT_CHAR_LIMIT = 12000

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

# Sentinel mapping key for schemaless (general-mode) surveys: the whole PDF
# is stored as a single response under this id instead of being split into
# per-section answers. The commit step recognises it and writes the raw text
# without the "Q: ...\n\nA: ..." section wrapper.
FULLTEXT_PROMPT_ID = "__pdf_fulltext__"

# Above this length, an extracted answer block is treated as suspicious
# (likely we matched the wrong heading and swept multiple sections).
ANSWER_BLOCK_SOFT_MAX_CHARS = 8000

# These headings unambiguously introduce material that is not a student's
# structured-reflection answer. They remain active even when an older schema
# has not yet received an explicit pdf_ingest profile.
GLOBAL_PDF_STOP_HEADINGS = (
    "Submission Guidelines",
    "Raw Conversation Transcript",
)

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
#
# Two-layer strategy:
#   1. pdfplumber (table-aware) — detects ruled tables and splices a clean
#      Markdown pipe table back into the text *at the table's position*, so
#      roster / Likert-matrix / prompt-response tables in the reflection
#      templates survive as structure the frontend can render natively
#      instead of being flattened into jumbled prose.
#   2. pypdf (flat text) — the original extractor; owns the friendly
#      encrypted / image-only error messages and is the fallback whenever
#      pdfplumber is unavailable, errors, or yields nothing.
# Table support is therefore strictly additive: worst case we get exactly
# the previous pypdf behaviour.

_PDF_TABLE_MIN_ROWS = 2
_PDF_TABLE_MIN_COLS = 2
_WS_RUN_RE = re.compile(r"\s+")


class _PdfPlumberUnavailable(Exception):
    """pdfplumber (or a dependency) isn't importable in this environment."""


def _cell_text(value: Any) -> str:
    """Normalise a pdfplumber cell (may be ``None`` or multi-line) to a
    single whitespace-collapsed string."""
    if value is None:
        return ""
    return _WS_RUN_RE.sub(" ", str(value)).strip()


def _merge_continuation_rows(rows: list[list[str]]) -> list[list[str]]:
    """Fold rows whose first column is empty up into the previous row.

    A row with an empty leading (label) column but content elsewhere is a
    wrapped cell that the table finder split onto its own line — e.g. a
    "Rating / (1–5)" two-line header, or a justification that wrapped. We
    reattach its cells (column-wise, space-joined) to the row above so the
    grid stays one logical row per record. A real record always carries its
    own row label, so this won't swallow genuine data.
    """
    if len(rows) <= 1:
        return rows
    out: list[list[str]] = [list(rows[0])]
    for row in rows[1:]:
        if out and not row[0] and any(row[1:]):
            prev = out[-1]
            for i in range(1, len(row)):
                if row[i]:
                    prev[i] = f"{prev[i]} {row[i]}".strip() if prev[i] else row[i]
        else:
            out.append(list(row))
    return out


def _clean_table(raw: list[list[Any]] | None) -> list[list[str]] | None:
    """Tidy a raw pdfplumber table into a dense grid for Markdown.

    pdfplumber's ``lines`` strategy routinely emits artifact rows/columns:
    fully-empty separator rows, a header word landing in a *different*
    physical column than its body values (because header and body cells are
    divided by different rulings), and wrapped cells split onto extra rows.
    This:

    1. stringifies + whitespace-collapses every cell,
    2. drops fully-empty rows,
    3. merges adjacent columns whose non-empty cells never collide in the
       same row — re-uniting a header column with its split-off value
       column. This is a general structural fix, not template-specific:
       genuine multi-column data collides constantly and is never merged.
    4. drops fully-empty columns,
    5. folds empty-first-column continuation rows up into the prior row
       (reassembles wrapped cells / two-line headers).

    Returns ``None`` if the result isn't a real >=2x2 table, so a degenerate
    detection is ignored and its page region stays as plain extracted text.
    """
    rows = [[_cell_text(c) for c in (row or [])] for row in (raw or [])]
    ncols = max((len(r) for r in rows), default=0)
    if ncols == 0:
        return None
    rows = [r + [""] * (ncols - len(r)) for r in rows]   # pad ragged rows
    rows = [r for r in rows if any(r)]                   # drop empty rows
    if len(rows) < _PDF_TABLE_MIN_ROWS:
        return None

    # Greedy left-to-right merge of non-colliding adjacent columns. Invariant:
    # after each merge, every row has at most one non-empty cell per group, so
    # collapsing a group loses no data.
    groups: list[list[int]] = []
    cur = [0]
    for j in range(1, ncols):
        collides = any(
            any(rows[r][k] for k in cur) and rows[r][j]
            for r in range(len(rows))
        )
        if collides:
            groups.append(cur)
            cur = [j]
        else:
            cur.append(j)
    groups.append(cur)

    merged: list[list[str]] = []
    for r in range(len(rows)):
        new_row: list[str] = []
        for grp in groups:
            vals = [rows[r][k] for k in grp if rows[r][k]]
            new_row.append(vals[0] if vals else "")
        merged.append(new_row)

    keep = [c for c in range(len(groups)) if any(row[c] for row in merged)]
    if len(keep) < _PDF_TABLE_MIN_COLS:
        return None
    dense = [[row[c] for c in keep] for row in merged]
    dense = _merge_continuation_rows(dense)
    if len(dense) < _PDF_TABLE_MIN_ROWS:
        return None
    return dense


def _table_to_markdown(table: list[list[str]]) -> str:
    """Render a cleaned grid as a GitHub-style Markdown pipe table. The first
    row is treated as the header."""
    ncols = max(len(r) for r in table)

    def fmt(row: list[str]) -> str:
        cells = [(row[i] if i < len(row) else "").replace("|", r"\|") for i in range(ncols)]
        return "| " + " | ".join(cells) + " |"

    lines = [fmt(table[0]), "| " + " | ".join(["---"] * ncols) + " |"]
    lines.extend(fmt(r) for r in table[1:])
    return "\n".join(lines)


def _extract_page_with_tables(page) -> str:
    """Extract one page's text in reading order, replacing each ruled table
    with a Markdown pipe table spliced in at its vertical position.

    Non-table text is pulled from the horizontal bands *between* tables, so
    table cell text is never double-counted and section headings sitting
    above/below a table stay with their prose.
    """
    try:
        found = page.find_tables()           # default "lines" strategy
    except Exception as e:  # pragma: no cover — pdfplumber internal edge
        logger.info("find_tables failed on a page: %s", e)
        found = []

    good: list[tuple[float, float, str]] = []  # (top, bottom, markdown)
    for t in found:
        try:
            cleaned = _clean_table(t.extract())
        except Exception:
            cleaned = None
        if cleaned:
            _x0, top, _x1, bottom = t.bbox
            good.append((float(top), float(bottom), _table_to_markdown(cleaned)))

    if not good:
        return page.extract_text() or ""

    good.sort(key=lambda g: g[0])
    width, height = page.width, page.height
    parts: list[str] = []

    def _band(top: float, bottom: float) -> None:
        if bottom - top <= 1:
            return
        try:
            band = page.crop((0, max(0.0, top), width, min(height, bottom)), strict=False)
            txt = band.extract_text() or ""
        except Exception:  # pragma: no cover — offset mediabox etc.
            txt = ""
        if txt.strip():
            parts.append(txt)

    prev_bottom = 0.0
    for top, bottom, md in good:
        _band(prev_bottom, top)
        parts.append(md)
        prev_bottom = max(prev_bottom, bottom)
    _band(prev_bottom, height)
    return "\n\n".join(parts)


def _extract_with_pdfplumber(blob: bytes) -> str:
    """Table-aware extraction. Returns ``""`` if nothing is extractable (the
    caller then falls back to pypdf). Raises ``_PdfPlumberUnavailable`` when
    the library can't be imported."""
    try:
        import pdfplumber
    except ImportError as e:
        raise _PdfPlumberUnavailable(str(e)) from e

    pages_text: list[str] = []
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        for page in pdf.pages:
            try:
                pages_text.append(_extract_page_with_tables(page))
            except Exception as e:  # pragma: no cover — per-page resilience
                logger.info("pdfplumber page extract failed: %s", e)
                try:
                    pages_text.append(page.extract_text() or "")
                except Exception:
                    pages_text.append("")
    return _normalise_text("\n\n".join(pages_text))


def _extract_pdf_text(blob: bytes) -> str:
    """Extract text from a PDF, with ruled tables rendered as Markdown.

    Tries table-aware pdfplumber extraction first; on any failure (library
    missing, parse error, or empty result) falls back to the pure-pypdf
    extractor, which owns the friendly encrypted / image-only error messages.

    Raises ``ValueError`` with a human-friendly message on parse failure.
    The view layer surfaces the message back to the instructor.
    """
    if not blob:
        raise ValueError("Empty file.")
    try:
        text = _extract_with_pdfplumber(blob)
        if text:
            return text
        logger.info("pdfplumber returned no text; falling back to pypdf")
    except _PdfPlumberUnavailable as e:
        logger.info("pdfplumber unavailable; using pypdf: %s", e)
    except Exception as e:
        logger.info("pdfplumber extraction failed; using pypdf: %s", e)
    return _extract_pdf_text_pypdf(blob)


def _extract_pdf_text_pypdf(blob: bytes) -> str:
    """Flat-text PDF extraction via pypdf (the original extractor).

    Raises ``ValueError`` with a human-friendly message on parse failure
    (encrypted, corrupted, image-only).
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


# Schema-driven answer cleanup --------------------------------------------

def pdf_ingest_profile(schema_body: dict | None) -> dict:
    """Return a defensive, normalized PDF-ingest profile from schema JSON.

    Invalid optional fields are ignored rather than crashing a batch. The two
    global stop headings are always active because they are clear non-answer
    boundaries across the supported reflection exports.
    """
    raw = schema_body.get("pdf_ingest") if isinstance(schema_body, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    prefixes: dict[str, list[str]] = {}
    raw_prefixes = raw.get("section_instruction_prefixes")
    if isinstance(raw_prefixes, dict):
        for prompt_id, values in raw_prefixes.items():
            if not isinstance(values, list):
                continue
            cleaned_values = [str(value).strip() for value in values if isinstance(value, str) and value.strip()]
            if cleaned_values:
                prefixes[str(prompt_id)] = cleaned_values

    def _string_list(key: str) -> list[str]:
        value = raw.get(key)
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]

    stop_headings = list(dict.fromkeys([*GLOBAL_PDF_STOP_HEADINGS, *_string_list("stop_headings")]))
    suspicious_markers = list(dict.fromkeys([*GLOBAL_PDF_STOP_HEADINGS, *_string_list("suspicious_markers")]))
    soft_max = raw.get("answer_soft_max_chars", ANSWER_BLOCK_SOFT_MAX_CHARS)
    if not isinstance(soft_max, int) or isinstance(soft_max, bool) or soft_max <= 0:
        soft_max = ANSWER_BLOCK_SOFT_MAX_CHARS

    return {
        "section_instruction_prefixes": prefixes,
        "stop_headings": stop_headings,
        "suspicious_markers": suspicious_markers,
        "answer_soft_max_chars": soft_max,
    }


def _configured_prefix_pattern(prefix: str) -> re.Pattern[str] | None:
    """Build a start-anchored pattern tolerant of PDF line/bullet wrapping."""
    normalized = _normalise_text(prefix)
    tokens = re.findall(r"\S+", normalized)
    if not tokens:
        return None
    separator = r"(?:\s|[•●▪◦])+"
    return re.compile(r"^\s*" + separator.join(re.escape(token) for token in tokens), re.IGNORECASE)


def _standalone_heading_pattern(heading: str) -> re.Pattern[str]:
    words = [re.escape(word) for word in heading.split() if word]
    body = r"\s+".join(words)
    return re.compile(
        r"^[\t >#*\-–—]*" + body + r"\s*[:.\-–—]?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )


def clean_mapping(
    mapping: dict[str, str],
    prompts: list[dict],
    profile: dict | None,
) -> tuple[dict[str, str], list[str], dict[str, list[str]], dict[str, list[str]]]:
    """Remove configured template material and classify remaining risks.

    Returns ``(cleaned, low_conf_prompt_ids, cleanup_notes,
    attention_reasons)``. Reason codes are stable API values consumed by the
    instructor review UI: ``empty``, ``suspicious_marker``, and ``oversized``.
    """
    normalized_profile = pdf_ingest_profile({"pdf_ingest": profile or {}})
    prefixes = normalized_profile["section_instruction_prefixes"]
    stop_headings = normalized_profile["stop_headings"]
    suspicious_markers = normalized_profile["suspicious_markers"]
    soft_max = normalized_profile["answer_soft_max_chars"]

    cleaned: dict[str, str] = {}
    low_conf: list[str] = []
    cleanup_notes: dict[str, list[str]] = {}
    attention_reasons: dict[str, list[str]] = {}

    prompt_ids = [str(prompt.get("prompt_id") or "") for prompt in prompts]
    for prompt_id in prompt_ids:
        block = _normalise_text(str((mapping or {}).get(prompt_id) or ""))
        notes: list[str] = []

        for configured_prefix in prefixes.get(prompt_id, []):
            pattern = _configured_prefix_pattern(configured_prefix)
            match = pattern.match(block) if pattern else None
            if match:
                block = block[match.end():].strip()
                notes.append("instruction_removed")
                break

        first_stop: re.Match[str] | None = None
        for heading in stop_headings:
            match = _standalone_heading_pattern(heading).search(block)
            if match and (first_stop is None or match.start() < first_stop.start()):
                first_stop = match
        if first_stop:
            block = block[:first_stop.start()].strip()
            notes.append("trailing_section_removed")

        reasons: list[str] = []
        if not block:
            reasons.append("empty")
        else:
            if any(_standalone_heading_pattern(marker).search(block) for marker in suspicious_markers):
                reasons.append("suspicious_marker")
            if len(block) > soft_max:
                reasons.append("oversized")

        cleaned[prompt_id] = block
        if notes:
            cleanup_notes[prompt_id] = notes
        if reasons:
            low_conf.append(prompt_id)
            attention_reasons[prompt_id] = reasons

    return cleaned, low_conf, cleanup_notes, attention_reasons


# AI-assisted mapping fallback --------------------------------------------

def _ai_assist_mapping(
    text: str,
    prompts: list[dict],
    low_conf_prompt_ids: list[str],
) -> dict[str, str]:
    """Ask OpenAI to map the extracted text to the survey's prompts for
    those flagged low-confidence by regex.

    Used as a second pass when heading-based regex matching missed
    sections — usually because the student's PDF used different wording
    than the schema (e.g. 'My Methods This Week' instead of 'Methods in
    Practice'). The model gets the full extracted text + the list of
    prompts that need answers, and returns a JSON map. Empty-string
    values mean 'I couldn't find an answer for this prompt either'.

    Returns
    -------
    {prompt_id: answer_text} for every low-conf prompt. Failure or empty
    returns an empty dict (caller falls back to the regex result).
    """
    if not text or not low_conf_prompt_ids:
        return {}
    relevant = [p for p in prompts if p.get("prompt_id") in low_conf_prompt_ids]
    if not relevant:
        return {}

    # Trim huge texts so we stay well inside the model's context and
    # keep cost predictable.
    if len(text) > AI_ASSIST_TEXT_CHAR_LIMIT:
        text = text[:AI_ASSIST_TEXT_CHAR_LIMIT] + "\n\n[…truncated for AI-assist pass]"

    properties = {}
    for p in relevant:
        properties[p["prompt_id"]] = {
            "type": "string",
            "description": (
                f"The student's answer to: '{p.get('opening_prompt', p.get('title', p['prompt_id']))}'. "
                "Empty string if no answer is found in the text."
            ),
        }
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }
    prompt_lines = "\n".join(
        f"- {p['prompt_id']}: {p.get('title') or p['prompt_id']}"
        f" — {p.get('opening_prompt', '')}".strip()
        for p in relevant
    )
    system = (
        "You are extracting student reflection answers from a PDF. The student "
        "wrote a free-form reflection that should answer specific prompts, but "
        "section headings may not exactly match. For each prompt below, find "
        "the corresponding answer in the text and return it verbatim. If no "
        "answer exists for a prompt, return an empty string for that prompt."
    )
    user = (
        f"PROMPTS NEEDING ANSWERS:\n{prompt_lines}\n\n"
        f"STUDENT'S REFLECTION TEXT:\n{text}"
    )
    try:
        result = openai_client.run_structured(
            chat_history=[{"role": "system", "content": system}],
            user_text=user,
            json_schema=schema,
            schema_name="pdf_ingest_assist",
            model=AI_ASSIST_MODEL,
            temperature=0,
        )
    except Exception as e:
        logger.info("AI-assist mapping failed (non-fatal): %s", e)
        return {}

    parsed = (result or {}).get("parsed") or {}
    out: dict[str, str] = {}
    for pid in low_conf_prompt_ids:
        v = parsed.get(pid, "")
        if isinstance(v, str) and v.strip():
            out[pid] = v.strip()
    return out


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

class IngestJobConflict(Exception):
    """Raised when starting a job while another job is still active for
    the same survey. Carries the existing job's id so the view layer can
    surface it back to the client (frontend can resume polling instead
    of starting over).
    """

    def __init__(self, existing_job: "LEAIPdfIngestJob"):
        super().__init__("Another ingest job is already in progress for this survey.")
        self.existing_job = existing_job


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

    Raises
    ------
    ValueError
        On bad input (size, count, missing attribution).
    IngestJobConflict
        When another live (pending/running, non-stale) job exists for
        the same survey. Concurrent workers on the same survey would
        race when committing, and they're never the user's intent —
        we surface the existing job_id so the client can resume.
    """
    # Reject if a non-stale job is already active for this survey.
    # Stale jobs (past PDF_INGEST_JOB_STALE_SECONDS) are treated as dead
    # and silently ignored — the new job supersedes them.
    active = (
        LEAIPdfIngestJob.objects
        .filter(survey=survey, status__in=[
            LEAIPdfIngestJob.STATUS_PENDING,
            LEAIPdfIngestJob.STATUS_RUNNING,
        ])
        .order_by("-created_at")
        .first()
    )
    if active and not is_job_stale(active):
        raise IngestJobConflict(active)
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
            profile = pdf_ingest_profile(schema_body)
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
                    "cleanup_notes": {},
                    "attention_reasons": {},
                    "preamble": "",
                    "error": "",
                }
                try:
                    text = _extract_pdf_text(blob)
                    if not prompts:
                        # Schemaless (general-mode) survey: store the whole PDF
                        # as a single response. No section mapping, no AI assist,
                        # nothing for the instructor to review by exception.
                        item["extracted_text"] = text
                        item["mapping"] = {FULLTEXT_PROMPT_ID: text}
                        item["low_conf_prompts"] = []
                        item["cleanup_notes"] = {}
                        item["attention_reasons"] = {}
                        item["ai_assisted_prompts"] = []
                        item["preamble"] = ""
                        item["status"] = "ok"
                        items.append(item)
                        LEAIPdfIngestJob.objects.filter(pk=job_pk).update(
                            items=items,
                            progress={"processed": idx + 1, "total": len(files_in_memory)},
                        )
                        continue
                    mapping, low_conf, preamble = map_text_to_prompts(text, prompts)
                    # Second pass: ask the AI to fill in the gaps the
                    # regex matcher couldn't find. Best-effort — failure
                    # leaves regex result intact, success upgrades the
                    # affected cells from empty/low-conf to ai_assisted.
                    ai_filled: dict[str, str] = {}
                    if low_conf:
                        try:
                            ai_filled = _ai_assist_mapping(text, prompts, low_conf)
                        except Exception as e:
                            logger.info("ai-assist non-fatal failure for %s: %s", filename, e)
                    if ai_filled:
                        for pid, val in ai_filled.items():
                            mapping[pid] = val
                        # Remove from low_conf the prompts AI was able to fill;
                        # keep the ones AI also missed flagged for the human.
                        low_conf = [pid for pid in low_conf if pid not in ai_filled]
                    mapping, cleanup_low, cleanup_notes, attention_reasons = clean_mapping(
                        mapping, prompts, profile,
                    )
                    low_conf = list(dict.fromkeys([*low_conf, *cleanup_low]))
                    for pid in low_conf:
                        if pid not in attention_reasons:
                            attention_reasons[pid] = [
                                "empty" if not mapping.get(pid) else "uncertain_mapping"
                            ]
                    item["extracted_text"] = text
                    item["mapping"] = mapping
                    item["low_conf_prompts"] = low_conf
                    item["cleanup_notes"] = cleanup_notes
                    item["attention_reasons"] = attention_reasons
                    item["ai_assisted_prompts"] = list(ai_filled.keys())
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
        ``{student_id: 'replace'|'skip'|'add'}``. If an existing student's
        decision is omitted, the safe default is ``skip`` (keep existing).

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

    # Validate and clean the browser-confirmed payload before any delete or
    # insert. The job preview is a convenience, not a trust boundary.
    preview_filenames = {
        str(item.get("filename") or "") for item in (job.items or [])
        if isinstance(item, dict)
    }
    allowed_prompt_ids = set(prompt_titles)
    if not prompts:
        allowed_prompt_ids = {FULLTEXT_PROMPT_ID}
    profile = pdf_ingest_profile(schema_body)
    seen_students: dict[str, str] = {}
    seen_filenames: set[str] = set()
    validated_items: list[dict] = []

    for raw_item in confirmed_items:
        if not isinstance(raw_item, dict):
            raise ValueError("Each confirmed PDF item must be an object.")
        filename = str(raw_item.get("filename") or "")
        sid = str(raw_item.get("student_id") or "").strip()
        skip = bool(raw_item.get("skip"))
        if filename not in preview_filenames:
            raise ValueError(f"Unknown PDF in commit: {filename or 'unnamed file'}.")
        if filename in seen_filenames:
            raise ValueError(f"PDF appears more than once in commit: {filename}.")
        seen_filenames.add(filename)
        if sid and not skip:
            if sid in seen_students:
                raise ValueError(
                    f"Student {sid} is attributed to more than one PDF "
                    f"({seen_students[sid]} and {filename})."
                )
            seen_students[sid] = filename

        mapping = raw_item.get("mapping")
        if not isinstance(mapping, dict):
            raise ValueError(f"Invalid answer mapping for {filename}.")
        unknown_prompts = sorted(set(str(pid) for pid in mapping) - allowed_prompt_ids)
        if unknown_prompts:
            raise ValueError(f"Unknown section for {filename}: {unknown_prompts[0]}.")

        accepted_long = {
            str(pid) for pid in (raw_item.get("accepted_long_prompts") or [])
            if isinstance(pid, (str, int, float))
        }
        if prompts and not skip:
            cleaned_mapping, _low, _notes, reasons = clean_mapping(mapping, prompts, profile)
            for prompt_id, prompt_reasons in reasons.items():
                if "suspicious_marker" in prompt_reasons:
                    raise ValueError(
                        f"{filename} section {prompt_id} still contains other PDF content. "
                        "Edit or skip this PDF before committing."
                    )
                if "oversized" in prompt_reasons and prompt_id not in accepted_long:
                    raise ValueError(
                        f"{filename} section {prompt_id} is unusually long. "
                        "Inspect and explicitly accept it, edit it, or skip the PDF."
                    )
            mapping = cleaned_mapping
        else:
            mapping = {str(pid): str(answer or "").strip() for pid, answer in mapping.items()}

        validated = dict(raw_item)
        validated["filename"] = filename
        validated["student_id"] = sid
        validated["mapping"] = mapping
        validated["skip"] = skip
        validated["accepted_long_prompts"] = sorted(accepted_long)
        validated_items.append(validated)

    confirmed_items = validated_items

    candidate_students = {
        item["student_id"] for item in confirmed_items
        if item["student_id"] and not item["skip"]
    }
    existing_pdf_students = set(
        FeedbackMessage.objects.filter(
            gpt_id=survey.id,
            student_id__in=candidate_students,
            source=FeedbackMessage.SOURCE_PDF,
        ).values_list("student_id", flat=True)
    )

    def _dedup_decision(student_id: str) -> str:
        decision = dedup_decisions.get(student_id)
        if decision is None:
            return "skip" if student_id in existing_pdf_students else "add"
        if decision not in {"add", "skip", "replace"}:
            raise ValueError(f"Invalid duplicate decision for {student_id}.")
        return decision

    items_summary: list[dict] = []
    student_ids_committed: set[str] = set()
    messages_to_create: list[FeedbackMessage] = []
    student_replace_targets: set[str] = set()

    # Resolve dedup decisions ahead of any writes.
    for item in confirmed_items:
        sid = (item.get("student_id") or "").strip()
        if not sid or item.get("skip"):
            continue
        decision = _dedup_decision(sid)
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
            decision = _dedup_decision(sid) if sid else "add"

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
                if prompt_id == FULLTEXT_PROMPT_ID:
                    # Schemaless survey: the whole PDF is one response.
                    content = answer
                else:
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
