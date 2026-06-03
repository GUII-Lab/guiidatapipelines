"""LEAI analysis business logic module.

All corpus-building, prompt/schema definitions, citation parsing, and
LLM orchestration for the LEAI Feedback Chat and Quick Take features live
here.  Views and API endpoints should import from this module rather than
duplicating logic.

Functions are grouped into:
  - Pure helpers (no DB, no LLM)
  - DB query helpers (no LLM)
  - LLM flow functions (may write to DB)
"""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Character budget per chunk when the corpus is too large to summarise in a
# single LLM call. ~40k chars ≈ ~10k tokens.
QUICKTAKE_CHUNK_CHAR_LIMIT = 40_000
QUICKTAKE_CHUNK_MAX_WORKERS = 5

# A bullet backed by this many distinct students (or fewer) is treated as an
# isolated one-off rather than a theme. The instructor UI splits these into a
# separate "Mentioned by one student" group so a single voice can't read as a
# class-wide pattern. Raise to 2 to require 3+ students before a claim counts
# as a theme.
ISOLATED_SUPPORT_MAX = 1

# Model for Quick Take / Instructor Insights calls (summariser + reducer +
# verifier + bullet-coherence pass). These judgments — especially detecting
# when a bullet bundles two distinct one-offs into a fake theme — benefit from
# a reasoning model, and the Quick Take job is async so it is no longer bound
# by the Heroku 30s request budget. We therefore use the reasoning model
# (gpt-5.1) rather than trading reasoning depth for throughput.
QUICKTAKE_MODEL = "gpt-5.1"

# If a quicktake row stays in pending/running past this many seconds past
# job_started_at, the web tier treats it as a zombie (dyno cycled mid-job)
# and lets a fresh generate call replace it.
QUICKTAKE_JOB_STALE_SECONDS = 600

from datapipeline import openai_client
from datapipeline.models import (
    Course,
    FeedbackGPT,
    FeedbackMessage,
    LEAIChatMessage,
    LEAIChatSession,
    LEAIQuickTake,
)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def default_quicktake_system_prompt() -> str:
    """Return the default system prompt used for Quick Take generation."""
    return (
        "You are an educational data analyst helping an instructor understand "
        "patterns in anonymous student feedback collected via LEAI "
        "(Learning Experience AI).\n\n"
        "You will be given a corpus of student responses, each identified by "
        "a response ID (e.g. R1, R2, ...).\n\n"
        "Your task is to synthesise the key themes, concerns, and insights "
        "into a concise set of bullet points. Each bullet captures one claim "
        "the instructor should see at a glance.\n\n"
        "Evidence rules — these are non-negotiable:\n"
        "  1. Each bullet must include 1–3 representative quotes drawn "
        "VERBATIM from the cited responses — a curated subset, NOT one per "
        "cited id. Do not paraphrase; copy a contiguous span from the "
        "response text. Quotes are how the instructor verifies a claim "
        "without clicking pills.\n"
        "  2. `cited_ids` must list EVERY response ID that genuinely "
        "supports the claim — not a sample. The instructor reads the "
        "length of this list as how many students are on this side, so a "
        "claim backed by 20 responses must cite all 20 of them. Do not "
        "truncate cited_ids to keep it short; the quotes array (rule 1) is "
        "where you stay concise.\n"
        "  3. Only reference response IDs that genuinely support the bullet. "
        "Never invent an R-id.\n"
        "  4. Each quote's `rid` field must be one of the IDs in `cited_ids`.\n"
        "  5. Each bullet must include a `sentiment` tag classifying the "
        "valence of what students said in this bullet:\n"
        "       - `positive` — students expressed satisfaction, praise, "
        'what is working, "this helped me", confidence gains.\n'
        "       - `negative` — students expressed frustration, confusion, "
        "struggle, dissatisfaction, things that aren't working, "
        "pain points, unmet needs.\n"
        "       - `mixed` — the bullet genuinely captures both positive "
        "and negative signals from the cited responses (e.g. \"clear "
        "lectures but pacing felt rushed\").\n"
        "       - `neutral` — purely descriptive or observational with no "
        "clear valence (e.g. a behavior pattern or factual report).\n"
        "     Classify by the tone of the cited student responses, not by "
        "the instructor's perspective. If unsure between `mixed` and one "
        "side, use `mixed` only when both signals are roughly equal; "
        "otherwise pick the dominant valence. Avoid defaulting to "
        "`neutral` when there is a real positive or negative signal.\n"
        "  6. One claim per bullet, and EVERY response you cite on a bullet "
        "must actually express that same claim. Do not bundle different "
        "observations — even related-sounding ones — under a single umbrella "
        "bullet to make them look like a shared theme: if two students raised "
        "genuinely different points, that is two bullets, not one. A point "
        "only one student made is a one-off, not a theme — give it its own "
        "single bullet (cited to that one response), and never reuse the same "
        "quote or response as the evidence for more than one bullet. "
        "Single-student bullets are grouped separately downstream, so there "
        "is no need to merge or stretch responses into a class-wide claim. "
        "Concretely: NEVER write a bullet like \"students feel disconnected "
        "and think design is only about pretty interfaces\" that cites one "
        "response for the first half and a different response for the second "
        "— those are two separate one-offs, each its own single bullet. If a "
        "bullet joins two different observations with \"and\", split it.\n\n"
        "Output format (JSON, enforced by schema):\n"
        '  { "bullets": [ { "text": "...", "cited_ids": ["R5","R12"], '
        '"quotes": [ {"rid":"R5","text":"verbatim span..."}, ... ], '
        '"sentiment": "negative" }, ... ] }\n\n'
        "Be objective and accurate. Avoid over-generalisation: never let one "
        "response stand in for the class, and weight each theme by how many "
        "distinct students actually voiced it.\n\n"
        "Also produce these additional fields:\n\n"
        "GAPS — topics the instructor might expect to hear about that are "
        "noticeably absent from the corpus. Each gap is an object "
        "{topic, note} where `topic` is the missing theme (e.g. "
        '"workload balance") and `note` is a short observation about its '
        "absence. Only flag gaps that genuinely matter; return an empty "
        "`gaps` array otherwise. Never invent content for a gap.\n\n"
        "FORM_SECTIONS — only emit when the corpus contains form-mode "
        "responses (look for indented `· Section Title: ...` lines under "
        "an rid). Each distinct section title gets one entry with a "
        "short `summary` of how students answered that area and one "
        "representative verbatim `quote` ({rid, text}). Return an empty "
        "array when no form-mode data is present.\n\n"
        "TEAM_HEALTH — only emit when the corpus contains team-tagged "
        "responses (look for the `[Rn · TeamName]` prefix on response "
        "lines). For each distinct team in scope, produce one entry with "
        "`team_name`, `status` (one of: healthy, watch, at_risk, "
        "no_response), a one-sentence `summary`, and a representative "
        "`quote` ({rid, text} — verbatim span). Use `no_response` for "
        "teams whose members didn't write enough to judge. If the corpus "
        "has no team-tagged responses, return an empty `team_health` "
        "array. Never invent team names; only use the labels shown in "
        "the prompt."
    )


def _chat_prompt_mode_addendum(corpus: list[dict]) -> str:
    """Return mode-specific instructions appended to the chat prompt.

    Inspects the corpus to detect whether it contains team-tagged
    responses (group-mode) and/or section-tagged responses (form-mode),
    then adds focused guidance for that surface. The result is empty for
    plain open-ended corpora.
    """
    teams_in_scope = sorted({
        e["team_name"] for e in corpus if e.get("team_name")
    })
    sections_in_scope = sorted({
        s.get("title", "")
        for e in corpus
        for s in (e.get("sections") or [])
        if s.get("title")
    })
    parts: list[str] = []
    if teams_in_scope:
        parts.append(
            "\n\nTEAM-MODE SCOPE: each response is tagged "
            "`[Rn · TeamName]`. When answering team-related questions, "
            "anchor your claims to specific teams. If the question is "
            'about which teams need attention, lead with team names '
            '("Echo and Hotel need…") and group evidence under each team. '
            f"Teams visible in this scope: {', '.join(teams_in_scope)}."
        )
    if sections_in_scope:
        parts.append(
            "\n\nFORM-MODE SCOPE: responses are split into FormSchema "
            "sections, rendered as indented `· Section Title: ...` lines "
            "under each rid. When relevant, organize your answer by "
            "section and reference section titles inline (e.g. \"In "
            "*Methods in Practice*, students…\"). Use section context to "
            "pick the most representative quote spans. Sections visible "
            f"in this scope: {', '.join(sections_in_scope)}."
        )
    return "".join(parts)


def default_chat_system_prompt(corpus: Optional[list[dict]] = None) -> str:
    """Return the default system prompt used for Feedback Chat turns.

    If `corpus` is provided, appends mode-specific instructions
    (team-mode or form-mode) so the model's answer shape matches the
    surface the instructor is looking at.
    """
    base = (
        "You are LEAI (Learning Experience AI), an educational analytics "
        "assistant that helps instructors explore and understand anonymous "
        "student feedback.\n\n"
        "You have access to a corpus of student responses shown below. "
        "When making claims, cite response IDs inline using square-bracket "
        "notation like [R17] or [R3]. Always use this exact format — each "
        "citation must be a separate [R<number>] tag, never bold, never "
        "comma-separated inside brackets.\n\n"
        "For EVERY response ID you cite in the answer, also include a "
        "matching entry in the `quotes` array. Each entry's `text` must be "
        "a VERBATIM span copied from that response — do not paraphrase. "
        "Pick the span that most directly backs your claim. This is what "
        "the instructor sees when they hover a citation, so the quote "
        "must do the work of justifying the cited rid on its own.\n\n"
        "Prefer 2–3 representative citations per claim over a long list. "
        "If many responses agree, pick the most exemplary 2–3 rather than "
        "naming all of them. Quality of evidence beats quantity.\n\n"
        "Output format (JSON, enforced by schema):\n"
        '  { "answer": "...[R5] markdown prose with citations...", '
        '"quotes": [ {"rid":"R5","text":"verbatim span..."}, ... ] }\n\n'
        "Be thoughtful, evidence-based, and pedagogically sensitive. "
        "Do not reveal individual student identities — all responses are "
        "anonymous."
    )
    if corpus:
        return base + _chat_prompt_mode_addendum(corpus)
    return base


# ---------------------------------------------------------------------------
# JSON Schemas
# ---------------------------------------------------------------------------

QUICKTAKE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "cited_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    # Quote-led evidence: each cited rid must come with a
                    # verbatim span from that response. This is what the UI
                    # surfaces to instructors so they can verify a claim in
                    # seconds without clicking 30 pills. Validated against
                    # the corpus after generation; unverifiable quotes are
                    # dropped.
                    "quotes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rid": {"type": "string"},
                                "text": {"type": "string"},
                            },
                            "required": ["rid", "text"],
                            "additionalProperties": False,
                        },
                    },
                    # Per-bullet valence so the instructor UI can color-code
                    # the takeaway list at a glance. "positive" = what's
                    # working, "negative" = what's struggling, "mixed" =
                    # both signals in the same bullet, "neutral" = purely
                    # descriptive/observational. The frontend treats an
                    # unknown or absent value as neutral.
                    "sentiment": {
                        "type": "string",
                        "enum": ["positive", "negative", "mixed", "neutral"],
                    },
                },
                "required": ["text", "cited_ids", "quotes", "sentiment"],
                "additionalProperties": False,
            },
        },
        # Phase 5: topics that surprisingly didn't surface in responses.
        # The model is asked to flag concepts the instructor might expect
        # to see (e.g. "workload balance", "lectures") that received no
        # mention. Each entry is a short note, not a claim.
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["topic", "note"],
                "additionalProperties": False,
            },
        },
        # Phase 8: per-section rollup for form-mode surveys. The corpus
        # builder splits each form session by FormSchema area
        # ("Area N of K — Title." delimiters). The model produces one
        # entry per distinct section title found in the corpus, with a
        # short summary of how students answered that section and one
        # representative verbatim quote. Empty when no form-mode data.
        "form_sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section_title": {"type": "string"},
                    "summary": {"type": "string"},
                    "quote": {
                        "type": "object",
                        "properties": {
                            "rid": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["rid", "text"],
                        "additionalProperties": False,
                    },
                },
                "required": ["section_title", "summary", "quote"],
                "additionalProperties": False,
            },
        },
        # Phase 7: per-team rollup, only meaningful when the corpus
        # spans group-mode surveys (build_response_corpus annotates each
        # entry with `team_name` when an assignment exists). For every
        # team in scope, the model produces a status tag and a
        # representative quote. Empty when no team data exists.
        "team_health": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["healthy", "watch", "at_risk", "no_response"],
                    },
                    "summary": {"type": "string"},
                    "quote": {
                        "type": "object",
                        "properties": {
                            "rid": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["rid", "text"],
                        "additionalProperties": False,
                    },
                },
                "required": ["team_name", "status", "summary", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "bullets", "gaps", "team_health", "form_sections",
    ],
    "additionalProperties": False,
}

CHAT_TURN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        # Markdown prose answer with inline [R<n>] citation tags, same as
        # the pre-Phase-6 chat format. The frontend already knows how to
        # render that.
        "answer": {"type": "string"},
        # For each unique cited rid, the model supplies ONE verbatim quote
        # span from that response. The frontend shows this in the citation
        # popover instead of dumping the full session transcript, so the
        # instructor sees exactly the evidence the model is leaning on.
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rid": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["rid", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answer", "quotes"],
    "additionalProperties": False,
}

VERIFIER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "bullet_index": {"type": "integer"},
                    "source_id": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["supported", "unsupported", "partial"],
                    },
                },
                "required": ["bullet_index", "source_id", "verdict"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}

# Bullet-level coherence: is each themed bullet ONE shared claim, or a bundle
# of distinct points different responses made separately? Drives the split in
# enforce_coherence so two one-offs can't masquerade as a multi-student theme.
COHERENCE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "bullet_index": {"type": "integer"},
                    "coherent": {"type": "boolean"},
                },
                "required": ["bullet_index", "coherent"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Corpus builder
# ---------------------------------------------------------------------------

# Form-mode AI agent prefixes each section with this header. The frontend
# `leai-formmode.js` engine emits exactly this pattern, so it's a reliable
# delimiter for splitting a session transcript into per-section answers.
# Example: "Area 2 of 4 — Methods in Practice. ..."
_FORM_SECTION_RE = re.compile(
    r"^Area\s+(\d+)\s+of\s+(\d+)\s+[—\-]\s+([^.\n]+)\.",
)


def _extract_form_sections(messages: list[dict]) -> list[dict]:
    """Given a session's interleaved transcript, group user messages by the
    form-schema section they answer.

    `messages` is a list of {role, content} dicts in chronological order.
    Returns `[{title, text}]` — one entry per section the student actually
    responded to, with their messages joined.

    Section boundaries are detected by the "Area N of K — Title." prefix
    the AI agent emits at the start of each section. User messages before
    the first such marker (e.g. greetings, "Next question") get assigned
    to a synthetic "Unsorted" section so they're never dropped.
    """
    current_title: Optional[str] = None
    bucket: dict[str, list[str]] = {}
    order: list[str] = []

    def _bump(title: str, text: str) -> None:
        if title not in bucket:
            bucket[title] = []
            order.append(title)
        bucket[title].append(text)

    for m in messages:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        role = m.get("role")
        if role == "assistant":
            match = _FORM_SECTION_RE.match(content)
            if match:
                current_title = match.group(3).strip()
            continue
        # user/student message
        title = current_title or "Unsorted"
        _bump(title, content)

    return [
        {"title": t, "text": " | ".join(bucket[t])}
        for t in order
    ]


def _surveys_in_scope(
    course: Course,
    scope_kind: str,
    scope_week_number: Optional[int] = None,
    scope_survey_ids: Optional[list] = None,
):
    """Resolve the FeedbackGPT queryset for a Quick Take scope.

    Single source of truth shared by build_response_corpus (which reads the
    responses) and compute_team_completeness (which reads the team rosters),
    so the two always agree on which surveys are in scope:
        "course" — every survey for the course
        "week"   — surveys whose week_number matches
        "custom" — surveys listed in scope_survey_ids (none when empty)
    """
    qs = FeedbackGPT.objects.filter(course=course)
    if scope_kind == "week":
        qs = qs.filter(week_number=scope_week_number)
    elif scope_kind == "custom":
        if scope_survey_ids:
            qs = qs.filter(pk__in=scope_survey_ids)
        else:
            qs = qs.none()
    return qs


def build_response_corpus(
    course: Course,
    scope_kind: str,
    scope_week_number: Optional[int] = None,
    scope_survey_ids: Optional[list] = None,
    scope_session_ids: Optional[list] = None,
) -> list[dict]:
    """Build a list of response dicts from FeedbackMessage records.

    Each entry has:
        rid           — "R1", "R2", ... (deterministic ordering)
        survey_id     — FeedbackGPT pk
        session_id    — FeedbackMessage.session_id
        week_number   — FeedbackGPT.week_number (may be None)
        text          — concatenated student messages for that session
        team_name     — populated for group-mode surveys (Phase 7)
        sections      — populated for form-mode surveys (Phase 8):
                        [{title, text}] per FormSchema area the student
                        actually responded to.

    Scope rules:
        "course"  — all FeedbackGPT surveys for this course
        "week"    — only surveys where week_number == scope_week_number
        "custom"  — surveys listed in scope_survey_ids OR sessions in scope_session_ids
    """
    # 1. Resolve the set of FeedbackGPT surveys in scope
    surveys_qs = _surveys_in_scope(
        course, scope_kind, scope_week_number, scope_survey_ids,
    )

    survey_map: dict[int, Optional[int]] = {
        s.pk: s.week_number for s in surveys_qs
    }
    survey_mode: dict[int, str] = {s.pk: s.mode for s in surveys_qs}
    survey_ids = list(survey_map.keys())

    # 2. Fetch messages. For non-form surveys we only need user turns; for
    # form surveys we need the AI turns too so we can detect the "Area N
    # of K — Title." delimiter and split a session into sections.
    form_survey_ids = {sid for sid, mode in survey_mode.items() if mode == "form"}
    nonform_survey_ids = [sid for sid in survey_ids if sid not in form_survey_ids]

    # PDF-ingested rows are stored with sent_by="student" (not a "user"
    # variant), so include source="pdf" explicitly — otherwise whole-PDF
    # responses uploaded to non-form surveys would be silently dropped from
    # the corpus and never reach Quick Take / FeedbackChat.
    user_only_qs = (
        FeedbackMessage.objects
        .filter(gpt_id__in=nonform_survey_ids)
        .filter(Q(sent_by__in=["user-message", "user"]) | Q(source=FeedbackMessage.SOURCE_PDF))
        .order_by("session_id", "created_at")
    )
    full_qs = (
        FeedbackMessage.objects
        .filter(gpt_id__in=form_survey_ids)
        .order_by("session_id", "created_at")
    )

    if scope_kind == "custom" and scope_session_ids:
        user_only_qs = user_only_qs.filter(session_id__in=scope_session_ids)
        full_qs = full_qs.filter(session_id__in=scope_session_ids)

    # 3. Group by session_id. For non-form sessions we keep a flat list of
    # user texts (current behavior). For form sessions we keep the whole
    # interleaved transcript so we can do section extraction below.
    sessions: dict[str, dict] = {}
    for msg in user_only_qs:
        sid = msg.session_id
        if sid not in sessions:
            sessions[sid] = {
                "gpt_id": msg.gpt_id,
                "texts": [],
                "transcript": None,  # non-form sessions skip the transcript
            }
        sessions[sid]["texts"].append(msg.content)

    for msg in full_qs:
        sid = msg.session_id
        if sid not in sessions:
            sessions[sid] = {
                "gpt_id": msg.gpt_id,
                "texts": [],
                "transcript": [],
            }
        role = (
            "user"
            if msg.sent_by in ("user", "user-message")
            else "assistant"
        )
        sessions[sid]["transcript"].append(
            {"role": role, "content": msg.content}
        )
        if role == "user":
            sessions[sid]["texts"].append(msg.content)

    # 4. Sort deterministically: week_number ASC (None last), then session_id lexical ASC
    def sort_key(item):
        sid, data = item
        week = survey_map.get(data["gpt_id"])
        return (week is None, week or 0, sid)

    sorted_sessions = sorted(sessions.items(), key=sort_key)

    # 5. For group-mode surveys, look up which team each session belongs
    # to (Phase 7). Sessions without an assignment get team_name=None.
    # Imported here to keep the function self-contained and avoid a
    # module-level dependency on the assignment models (they live in the
    # same app, but the leai_analysis module is otherwise model-light).
    from .models import SessionTeamAssignment
    sids = [sid for sid, _ in sorted_sessions]
    team_by_sid: dict[str, str] = {}
    if sids:
        assignments = (
            SessionTeamAssignment.objects
            .filter(session_id__in=sids)
            .select_related("survey_team__snapshot")
        )
        for a in assignments:
            t = a.survey_team
            name = t.display_name or f"{t.snapshot.label_prefix} {t.number}"
            team_by_sid[a.session_id] = name

    # 6. Build corpus entries with R-IDs
    corpus = []
    for idx, (sid, data) in enumerate(sorted_sessions, start=1):
        gpt_id = data["gpt_id"]
        # For form-mode sessions, split the transcript into per-section
        # answers using the agent's "Area N of K — Title." delimiter.
        sections: list[dict] = []
        if data.get("transcript"):
            sections = _extract_form_sections(data["transcript"])
        corpus.append({
            "rid": f"R{idx}",
            "survey_id": gpt_id,
            "session_id": sid,
            "week_number": survey_map.get(gpt_id),
            "team_name": team_by_sid.get(sid),
            "text": " | ".join(data["texts"]),
            "sections": sections,
        })

    return corpus


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _rid_line(entry: dict) -> str:
    """Render one corpus entry for inclusion in a prompt block.

    When the entry has a `team_name` (group-mode survey response), prepend
    the team label. When the entry has `sections` (form-mode response),
    render each section on its own indented line so the model can reason
    section-by-section instead of as a noisy concatenation.
    """
    team = entry.get("team_name")
    prefix = f"[{entry['rid']}]"
    if team:
        prefix = f"[{entry['rid']} · {team}]"

    sections = entry.get("sections") or []
    if sections:
        lines = [f"{prefix}"]
        for s in sections:
            title = s.get("title", "")
            text = s.get("text", "")
            lines.append(f"  · {title}: {text}")
        return "\n".join(lines)
    return f"{prefix} {entry['text']}"


def build_quicktake_user_text(
    course_name: str,
    corpus: list[dict],
    scope_label: str,
) -> str:
    """Build the user-turn text for a Quick Take structured call."""
    teams_present = sorted({e.get("team_name") for e in corpus if e.get("team_name")})
    sections_present = sorted({
        s.get("title", "")
        for e in corpus
        for s in (e.get("sections") or [])
        if s.get("title")
    })
    lines = [
        f"Course: {course_name}",
        f"Scope: {scope_label}",
        f"Total responses: {len(corpus)}",
    ]
    if teams_present:
        lines.append(
            f"Teams in scope ({len(teams_present)}): " + ", ".join(teams_present)
        )
    if sections_present:
        lines.append(
            f"Form sections in scope ({len(sections_present)}): "
            + ", ".join(sections_present)
        )
    lines += ["", "--- Student Responses ---"]
    for entry in corpus:
        lines.append(_rid_line(entry))
    lines.append("")
    lines.append(
        "Synthesise these responses into bullet points, citing response IDs "
        "inline.\n\n"
        "REQUIRED for every bullet:\n"
        '  - `quotes`: 1–3 verbatim spans copied character-for-character '
        "from the cited responses — a curated subset, NOT one per cited id. "
        "Do NOT paraphrase, summarise, or stitch together pieces from "
        "different rids. Copy a contiguous run of text from the rid's "
        "actual response. A bullet with zero quotes will be rejected. Do "
        "NOT reuse the same quote (same response and span) across bullets, "
        "and do not build more than one bullet around a single response.\n"
        '  - `cited_ids`: list EVERY response that genuinely supports this '
        "claim — not a sample. The instructor reads the length of this list "
        "as how many students are on this side, so cite all of them; keep "
        "`quotes` (above) short instead. Cite a response only if it actually "
        "expresses THIS bullet's claim — do not merge unrelated "
        "single-student points to inflate the count. If a bullet joins two "
        "different observations with \"and\" (e.g. \"X and Y\"), split it into "
        "separate bullets. Each quote's `rid` must "
        "appear in this bullet's `cited_ids`.\n"
        '  - `sentiment` must be one of `positive`, `negative`, `mixed`, or '
        "`neutral` (see the system prompt for the rubric). Only use "
        "`neutral` when the bullet is genuinely descriptive without "
        "positive or negative tone — do not default to it.\n\n"
        + (
            "REQUIRED for `form_sections`: produce one entry per distinct "
            "form section title listed above (those are the only valid "
            "section_title values). Empty array is only acceptable if no "
            "form sections appear in the responses.\n\n"
            if sections_present else ""
        )
        + (
            "REQUIRED for `team_health`: produce one entry per team listed "
            "above. Use status='no_response' for teams that did not submit.\n\n"
            if teams_present else ""
        )
        + "OPTIONAL: `tensions` for genuine disagreements (both sides need "
        "verbatim quotes). `gaps` for topics noticeably absent. Return empty "
        "arrays for these if you don't see clear evidence — never invent.\n\n"
        "REQUIRED: `actions` — 3–6 concrete, prioritised recommendations the "
        "instructor could act on mid-course, each with a one-line `rationale` "
        "and 1–3 `cited_ids`. Lead with `high`-priority friction. Empty array "
        "only if nothing is genuinely actionable."
    )
    return "\n".join(lines)


def build_chat_corpus_block(corpus: list[dict]) -> str:
    """Build the numbered response block appended to the chat system prompt."""
    lines = ["--- Student Response Corpus ---"]
    for entry in corpus:
        lines.append(_rid_line(entry))
    lines.append("--- End of Corpus ---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Citation parser
# ---------------------------------------------------------------------------

_CITATION_RE = re.compile(r"\[R(\d+)\]")
_COMMA_CITATION_RE = re.compile(r"\[(R\d+(?:\s*,\s*R\d+)+)\]")
_BOLD_CITATION_RE = re.compile(r"\*\*(R\d+)\*\*")


def _normalize_citations(text: str) -> str:
    """Normalize variant citation formats into standard [R<n>] form.

    Handles:
      - Comma-separated: [R5, R25, R35] → [R5][R25][R35]
      - Markdown bold:   **R18**         → [R18]
    """
    # Expand comma-separated lists first
    def expand_comma(match: re.Match) -> str:
        ids = [rid.strip() for rid in match.group(1).split(",")]
        return "".join(f"[{rid}]" for rid in ids)
    text = _COMMA_CITATION_RE.sub(expand_comma, text)

    # Convert bold R-ids to bracketed form
    text = _BOLD_CITATION_RE.sub(r"[\1]", text)
    return text


def parse_inline_citations(
    text: str,
    valid_rids: Optional[set[str]] = None,
) -> tuple[str, list[str]]:
    """Replace [R<n>] citations with stable [1], [2], ... pill indices.

    Returns:
        (cleaned_text, cited_list)

    Each unique R-id gets a 1-based pill index in the order it first appears.
    Repeated occurrences of the same R-id reuse that pill index, so the
    rendered text and the cited[] array always agree on pill_index → rid.

    Also handles comma-separated citations like [R5, R25, R35] by first
    expanding them into individual [R5][R25][R35] format.

    If `valid_rids` is provided, any [R<n>] whose rid is not in the set is
    dropped from the output and excluded from `cited_list`. This filters
    LLM-hallucinated citations (rids that don't exist in the corpus the
    model was shown) so the frontend never renders a pill that resolves to
    "Response text not available." Pass `valid_rids=None` to disable
    filtering (default, preserves prior behavior).
    """
    # Normalize variant citation formats first
    text = _normalize_citations(text)

    cited_order: list[str] = []  # ordered unique R-ids
    pill_by_rid: dict[str, int] = {}

    def replace(match: re.Match) -> str:
        rid = f"R{match.group(1)}"
        if valid_rids is not None and rid not in valid_rids:
            return ""  # drop hallucinated citation entirely
        pill = pill_by_rid.get(rid)
        if pill is None:
            cited_order.append(rid)
            pill = len(cited_order)
            pill_by_rid[rid] = pill
        return f"[{pill}]"

    cleaned = _CITATION_RE.sub(replace, text)
    return cleaned, cited_order


def _normalize_for_quote_match(s: str) -> str:
    """Lossy lowercase + whitespace-collapse for substring quote matching.

    Models often re-emit quotes with slightly different whitespace, smart
    quotes, or trailing punctuation. We don't require character-perfect
    equality — we require that the model's quote is a recognizable span
    of the source. Lowercasing and collapsing whitespace gives that
    flexibility without letting hallucinated content through.
    """
    # Normalize curly quotes / apostrophes that LLMs commonly emit.
    table = str.maketrans({
        "‘": "'", "’": "'",
        "“": '"', "”": '"',
        "–": "-", "—": "-",
    })
    s = s.translate(table).lower()
    # Collapse all whitespace runs (incl. newlines) to single spaces.
    return re.sub(r"\s+", " ", s).strip()


def validate_form_sections(
    form_sections: list[dict],
    corpus: list[dict],
) -> list[dict]:
    """Drop form_sections entries with unverifiable quotes or unknown titles.

    Only accepts section titles that actually appear on some corpus entry's
    `sections`, and only accepts quote text that's a substring (normalized)
    of the cited rid's full text. Together this stops the model from
    inventing fictional section names or fabricating quotes.
    """
    valid_titles: set[str] = set()
    for e in corpus or []:
        for s in e.get("sections") or []:
            t = (s.get("title") or "").strip()
            if t:
                valid_titles.add(t)

    corpus_by_rid = {e["rid"]: _normalize_for_quote_match(e["text"]) for e in corpus}

    kept: list[dict] = []
    for entry in form_sections or []:
        title = (entry.get("section_title") or "").strip()
        if title not in valid_titles:
            continue
        q = entry.get("quote") or {}
        source = corpus_by_rid.get(q.get("rid", ""))
        if source is None:
            continue
        qtext = q.get("text", "")
        if not qtext or _normalize_for_quote_match(qtext) not in source:
            continue
        kept.append(entry)
    return kept


def validate_team_health(
    team_health: list[dict],
    corpus: list[dict],
) -> list[dict]:
    """Drop team_health entries with unverifiable quotes or unknown team names.

    Only accepts team names that actually appear on a corpus entry, and
    only accepts quote text that's a substring (normalized) of the cited
    rid's response. Both guards together stop the model from inventing
    teams or fabricating quotes attributed to a real rid.
    """
    valid_teams = {e.get("team_name") for e in corpus if e.get("team_name")}
    corpus_by_rid = {e["rid"]: _normalize_for_quote_match(e["text"]) for e in corpus}
    kept: list[dict] = []
    for t in team_health or []:
        name = t.get("team_name", "")
        # `no_response` status doesn't need a verifiable quote — the model
        # is reporting absence — but the team name must still be real.
        status = t.get("status", "")
        if name not in valid_teams:
            continue
        q = t.get("quote") or {}
        if status == "no_response":
            kept.append(t)
            continue
        source = corpus_by_rid.get(q.get("rid", ""))
        if source is None:
            continue
        qtext = q.get("text", "")
        if not qtext or _normalize_for_quote_match(qtext) not in source:
            continue
        kept.append(t)
    return kept


def compute_team_completeness(
    corpus: list[dict],
    surveys_qs,
) -> dict[str, dict]:
    """Deterministic per-team response completeness for group-mode scope.

    Returns {team_name: {"expected": int, "submitted": int, "missing": int}}
    covering EVERY team in the in-scope surveys' SurveyTeamSnapshots —
    including teams nobody submitted for, so the UI can flag fully-silent
    teams rather than letting them vanish.

    expected  — the declared SurveyTeam.size (how many members the team has).
                Summed across snapshots when the scope spans multiple surveys
                (a multi-week scope expects one submission per member per
                survey).
    submitted — distinct submitting sessions in the corpus for that team. The
                corpus carries exactly one entry per session, so counting
                entries per team_name IS the submitter count.
    missing   — max(0, expected - submitted); clamped so roster drift (more
                submitters than the recorded size) can't go negative.

    Team names are reproduced with the SAME rule build_response_corpus uses
    (`display_name or "<snapshot.label_prefix> <number>"`) so they line up
    with the corpus team_name and the model's team_health entries.
    """
    from .models import SurveyTeamSnapshot

    submitted: dict[str, int] = {}
    for e in corpus:
        name = e.get("team_name")
        if name:
            submitted[name] = submitted.get(name, 0) + 1

    expected: dict[str, int] = {}
    snapshots = (
        SurveyTeamSnapshot.objects
        .filter(survey__in=list(surveys_qs))
        .prefetch_related("teams")
    )
    for snap in snapshots:
        for team in snap.teams.all():
            name = team.display_name or f"{snap.label_prefix} {team.number}"
            expected[name] = expected.get(name, 0) + (team.size or 0)

    out: dict[str, dict] = {}
    for name in set(expected) | set(submitted):
        exp = expected.get(name, 0)
        sub = submitted.get(name, 0)
        out[name] = {
            "expected": exp,
            "submitted": sub,
            "missing": max(0, exp - sub),
        }
    return out


def merge_team_completeness(
    team_health: list[dict],
    completeness: dict[str, dict],
) -> list[dict]:
    """Fold deterministic completeness counts into the model's team_health.

    - Every existing team_health entry gets its expected/submitted/missing
      counts attached (matched by team_name).
    - Every in-scope team the model did NOT assess is appended, so the
      instructor sees the full roster. A team nobody submitted for is flagged
      `at_risk` (the most important to surface); a partially-in team gets
      `watch`; a fully-submitted team the model skipped is `healthy`.
      Appended entries carry no summary/quote — the count line speaks for them.

    Sorted attention-first: at_risk → watch → healthy → no_response, then
    most-missing first, then by name, so silent and under-responding teams
    lead the list.
    """
    by_name = {(t.get("team_name") or ""): t for t in (team_health or [])}
    merged: list[dict] = []
    for t in team_health or []:
        c = completeness.get(t.get("team_name") or "")
        merged.append({**t, **c} if c else dict(t))

    for name, c in completeness.items():
        if name in by_name:
            continue
        if c["submitted"] == 0:
            status = "at_risk"
        elif c["missing"] > 0:
            status = "watch"
        else:
            status = "healthy"
        merged.append({
            "team_name": name,
            "status": status,
            "summary": "",
            "quote": {},
            **c,
        })

    status_rank = {"at_risk": 0, "watch": 1, "healthy": 2, "no_response": 3}
    merged.sort(key=lambda t: (
        status_rank.get(t.get("status") or "", 9),
        -int(t.get("missing", 0) or 0),
        t.get("team_name") or "",
    ))
    return merged


def validate_tensions(
    tensions: list[dict],
    corpus: list[dict],
) -> list[dict]:
    """Drop tensions whose side-quotes don't verify against the corpus.

    A tension is only useful if BOTH sides are grounded in real responses.
    If either side's quote can't be found in its cited rid (substring,
    normalized), the entire tension is dropped — a half-grounded
    disagreement is worse than no disagreement, because it signals
    conflict where there may be none.
    """
    corpus_by_rid = {e["rid"]: _normalize_for_quote_match(e["text"]) for e in corpus}
    kept: list[dict] = []
    for t in tensions or []:
        sides = t.get("sides") or []
        if len(sides) < 2:
            continue
        all_verified = True
        for side in sides:
            q = side.get("quote") or {}
            source = corpus_by_rid.get(q.get("rid", ""))
            if source is None:
                all_verified = False
                break
            qtext = q.get("text", "")
            if not qtext or _normalize_for_quote_match(qtext) not in source:
                all_verified = False
                break
        if all_verified:
            kept.append(t)
    return kept


def validate_quote_spans(
    bullets: list[dict],
    corpus: list[dict],
) -> tuple[list[dict], dict]:
    """Drop quotes whose text doesn't appear in the cited rid's response.

    For each bullet's `quotes[]`, check that `quote.text` is a substring
    (modulo normalization) of `corpus[quote.rid].text`. Unverifiable
    quotes are removed. Bullets aren't dropped — even with zero verified
    quotes the claim text may still be reasonable (the rid-level verifier
    handles those).

    Returns (cleaned_bullets, stats) where stats reports counts useful
    for monitoring how often the model hallucinates spans:
        {
          "quotes_total":    int,  # before filtering
          "quotes_verified": int,  # survived
          "quotes_dropped":  int,  # text not found in cited rid
          "quotes_orphaned": int,  # rid not in corpus at all
        }
    """
    corpus_by_rid = {e["rid"]: _normalize_for_quote_match(e["text"]) for e in corpus}
    stats = {"quotes_total": 0, "quotes_verified": 0, "quotes_dropped": 0,
             "quotes_orphaned": 0}

    cleaned: list[dict] = []
    for b in bullets:
        kept_quotes = []
        for q in b.get("quotes", []) or []:
            stats["quotes_total"] += 1
            rid = q.get("rid", "")
            qtext = q.get("text", "")
            if not rid or not qtext:
                stats["quotes_dropped"] += 1
                continue
            source = corpus_by_rid.get(rid)
            if source is None:
                stats["quotes_orphaned"] += 1
                continue
            if _normalize_for_quote_match(qtext) in source:
                kept_quotes.append(q)
                stats["quotes_verified"] += 1
            else:
                stats["quotes_dropped"] += 1
        cleaned.append({**b, "quotes": kept_quotes})

    return cleaned, stats


def filter_bullet_citations(
    bullets: list[dict],
    valid_rids: set[str],
) -> list[dict]:
    """Strip hallucinated rids from Quick Take bullets.

    The Quick Take prompt asks the model to emit a structured
    `{ text, cited_ids }` per bullet. The model occasionally cites rids
    that don't exist in the corpus it was shown; those would render as
    pills the frontend can't resolve. Drop them.

    Returns a new list (does not mutate input). Bullets whose `cited_ids`
    are all hallucinated end up with an empty `cited_ids` — kept rather
    than dropped because the claim text itself may still be reasonable
    (the verifier will flag it as unverified).
    """
    cleaned: list[dict] = []
    for b in bullets:
        kept = [rid for rid in b.get("cited_ids", []) if rid in valid_rids]
        cleaned.append({**b, "cited_ids": kept})
    return cleaned


def annotate_support(
    bullets: list[dict],
    isolated_max: int = ISOLATED_SUPPORT_MAX,
) -> list[dict]:
    """Tag each bullet with its student-support count and an isolation flag.

    `support_count` is the number of DISTINCT cited responses — the model is
    told to cite every response that supports a claim, so this is "how many
    students are on this side". `is_isolated` is True when that count is at
    most `isolated_max` (default 1), marking a single-student one-off that the
    frontend renders in a separate "Mentioned by one student" group instead of
    among the themes. Call this AFTER filter_bullet_citations so hallucinated
    rids don't inflate the count.

    Returns a new list; does not mutate the input.
    """
    annotated: list[dict] = []
    for b in bullets:
        count = len({rid for rid in b.get("cited_ids", []) if rid})
        annotated.append({
            **b,
            "support_count": count,
            "is_isolated": count <= isolated_max,
        })
    return annotated


def dedupe_bullet_quotes(bullets: list[dict]) -> list[dict]:
    """Stop one vivid quote from anchoring multiple bullets.

    Identical evidence — the same response (rid) quoted with the same span —
    reused across bullets is the reported failure mode where one student's
    striking line gets repeated on nearly every takeaway. Each quote, keyed by
    (rid, normalized text), is kept only under the FIRST bullet that uses it;
    later repeats are dropped. The same span from two DIFFERENT students is
    genuine corroboration and is left intact.

    A bullet that loses all its quotes is kept (its claim text and citation
    count still stand) — consistent with validate_quote_spans.

    Returns a new list; does not mutate the input.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for b in bullets:
        kept_quotes: list[dict] = []
        for q in b.get("quotes") or []:
            rid = (q.get("rid") or "").strip()
            key = (rid, _normalize_for_quote_match(q.get("text") or ""))
            if key in seen:
                continue
            seen.add(key)
            kept_quotes.append(q)
        out.append({**b, "quotes": kept_quotes})
    return out


def enforce_coherence(
    bullets: list[dict],
    incoherent_indices,
    verification: list[dict],
    isolated_max: int = ISOLATED_SUPPORT_MAX,
) -> tuple[list[dict], list[dict]]:
    """Split bundled bullets flagged incoherent into single-student one-offs.

    The per-claim count gate (`annotate_support`) is evaded when the model
    merges two DISTINCT single-student points into one bullet citing both rids
    — `support=2` makes it read as a class-wide theme even though neither
    student made the merged claim. Per-citation verification can't catch it:
    the bullet is phrased "some students feel X and [some] feel Y", so each
    response genuinely backs its own half and passes. `check_bullet_coherence`
    asks the orthogonal question — do ALL cited responses make the SAME point?
    — and reports the indices of bullets that are bundles.

    `incoherent_indices` is that set. Each flagged bullet citing >=2 responses
    is split into one single-student one-off per cited response, using that
    response's own verbatim quote as the claim, so neither point can
    masquerade as a shared theme. Other bullets are kept; `support_count` and
    `is_isolated` are derived from the distinct cited responses.

    `verification` (per (bullet_index, source_id) verdict) is re-indexed to the
    new bullet positions so the frontend's citation-dropping stays correct
    after a split. When `incoherent_indices` is empty (e.g. the coherence pass
    degraded to []), nothing is split — a strict superset of
    `annotate_support`, never a regression. Does not mutate the inputs.
    """
    incoherent = set(incoherent_indices or ())
    verdict_of: dict[tuple, Optional[str]] = {}
    for v in verification or []:
        verdict_of[(v.get("bullet_index"), v.get("source_id"))] = v.get("verdict")

    def distinct(seq):
        return list(dict.fromkeys(rid for rid in (seq or []) if rid))

    new_bullets: list[dict] = []
    new_verification: list[dict] = []

    for bi, b in enumerate(bullets):
        cited = distinct(b.get("cited_ids"))
        quote_by_rid: dict[str, dict] = {}
        for q in b.get("quotes") or []:
            rid = (q.get("rid") or "").strip()
            if rid and rid not in quote_by_rid:
                quote_by_rid[rid] = q

        is_bundle = bi in incoherent and len(cited) >= 2

        if is_bundle:
            # One self-standing one-off per cited response; its claim is the
            # student's own words, so nothing is synthesized across responses.
            for r in cited:
                q = quote_by_rid.get(r)
                claim = (q.get("text") or "").strip() if q else ""
                if not claim:
                    claim = (b.get("text") or "").strip()
                idx = len(new_bullets)
                new_bullets.append({
                    **b,
                    "text": claim,
                    "cited_ids": [r],
                    "quotes": [],
                    "support_count": 1,
                    "is_isolated": True,
                })
                new_verification.append({
                    "bullet_index": idx, "source_id": r, "verdict": "supported",
                })
        else:
            count = len(cited)
            idx = len(new_bullets)
            new_bullets.append({
                **b,
                "support_count": count,
                "is_isolated": count <= isolated_max,
            })
            for r in cited:
                vd = verdict_of.get((bi, r))
                if vd is not None:
                    new_verification.append({
                        "bullet_index": idx, "source_id": r, "verdict": vd,
                    })

    return new_bullets, new_verification


def filter_actions(
    actions: list[dict],
    valid_rids: set[str],
) -> list[dict]:
    """Clean Quick Take `actions`: drop hallucinated rids, normalize priority.

    Actions carry `cited_ids` (no verbatim quotes), so the only grounding
    check is rid existence — same contract as `filter_bullet_citations`.
    An action whose cited_ids are all hallucinated is kept with an empty
    list: the recommendation text may still be sound, and an action with
    no pills simply renders without citations. `priority` is coerced to a
    known value (defaulting to `medium`) so the UI never sees a stray tag.
    """
    valid_priority = {"high", "medium", "low"}
    cleaned: list[dict] = []
    for a in actions or []:
        if not (a.get("text") or "").strip():
            continue
        kept = [rid for rid in a.get("cited_ids", []) if rid in valid_rids]
        priority = (a.get("priority") or "medium").strip().lower()
        if priority not in valid_priority:
            priority = "medium"
        cleaned.append({**a, "cited_ids": kept, "priority": priority})
    return cleaned


# ---------------------------------------------------------------------------
# LLM flow: verify_claims
# ---------------------------------------------------------------------------

def verify_claims(
    corpus: list[dict],
    bullets: list[dict],
    model: Optional[str] = None,
) -> list[dict]:
    """Verify that bullet citations are supported by the corpus.

    Calls run_structured with VERIFIER_SCHEMA at temperature=0.

    Args:
        corpus: list of {rid, text, ...} dicts from build_response_corpus
        bullets: list of {text, cited_ids} dicts (from Quick Take or chat)

    Returns:
        list of {bullet_index, source_id, verdict} dicts
        On failure, returns [] (graceful degradation).
    """
    if not bullets or not corpus:
        return []

    # Build a compact corpus block for the verifier
    corpus_lines = [f"[{e['rid']}] {e['text']}" for e in corpus]
    corpus_block = "\n".join(corpus_lines)

    # Build bullet descriptions
    bullet_lines = []
    for idx, b in enumerate(bullets):
        cited = ", ".join(b.get("cited_ids", []))
        bullet_lines.append(f"Bullet {idx}: \"{b.get('text', '')}\" (cites: {cited})")
    bullets_block = "\n".join(bullet_lines)

    system_msg = (
        "You are a verification assistant. "
        "For each bullet point, judge whether each cited response expresses "
        "that bullet's claim. Use these verdicts strictly:\n"
        "- 'supported': the response, on its own, clearly states this bullet's "
        "FULL claim.\n"
        "- 'partial': the response is related but does not itself state the "
        "full claim — for instance it backs only ONE part of a claim that "
        "combines several points, so the claim only holds by stitching this "
        "response together with a different one.\n"
        "- 'unsupported': the response does not back the claim at all.\n"
        "Return a 'results' array where each entry has: "
        "bullet_index (int), source_id (the R-id string), "
        "and verdict ('supported', 'partial', or 'unsupported')."
    )

    user_text = (
        f"Corpus:\n{corpus_block}\n\n"
        f"Bullets to verify:\n{bullets_block}"
    )

    try:
        result = openai_client.run_structured(
            chat_history=[{"role": "system", "content": system_msg}],
            user_text=user_text,
            json_schema=VERIFIER_SCHEMA,
            schema_name="verification_result",
            model=model,
            temperature=0,
        )
        return result["parsed"].get("results", [])
    except Exception:
        # Graceful degradation: verification failure must not break the turn
        return []


def check_bullet_coherence(
    corpus: list[dict],
    bullets: list[dict],
    model: Optional[str] = None,
) -> list[dict]:
    """Judge whether each multi-citation bullet is ONE shared claim or a bundle.

    The count gate is evaded when the model merges two distinct single-student
    points into one bullet phrased "some students feel X and [some] feel Y" —
    each cited response then genuinely backs its own half, so per-citation
    verification passes it. This asks the orthogonal question: do ALL the cited
    responses make the SAME point (a real theme), or do different responses
    make different points (a bundle that should be split)?

    Only bullets citing >=2 distinct responses are evaluated; a single-citation
    bullet is trivially coherent and the model is not consulted when none
    qualify. Returns a list of {bullet_index, coherent: bool}. On failure
    returns [] (graceful: callers treat a missing judgment as coherent, so a
    verifier outage never splits a bullet).
    """
    if not bullets or not corpus:
        return []

    corpus_by_rid = {e["rid"]: e for e in corpus if e.get("rid")}
    blocks = []
    for idx, b in enumerate(bullets):
        cited = list(dict.fromkeys(r for r in (b.get("cited_ids") or []) if r))
        if len(cited) < 2:
            continue
        lines = [f"Bullet {idx}: \"{b.get('text', '')}\""]
        for r in cited:
            entry = corpus_by_rid.get(r)
            snippet = (entry["text"] if entry else "").strip()
            lines.append(f"   - {r}: {snippet}")
        blocks.append("\n".join(lines))
    if not blocks:
        return []

    system_msg = (
        "You check whether each themed bullet represents a SINGLE shared idea. "
        "For each bullet you are given its cited student responses. Answer "
        "coherent=true if the cited responses all make essentially the SAME "
        "point (a genuine shared theme). Answer coherent=false if the bullet "
        "lumps together DIFFERENT points that different responses make "
        "separately — for example \"students feel disconnected and think "
        "design is just about pretty interfaces\", where one response is about "
        "feeling disconnected and a different response is about design being "
        "only aesthetics. If the cited responses are about different topics, "
        "the bullet is NOT coherent. Return a 'results' array of objects "
        "{bullet_index (int), coherent (bool)}."
    )
    user_text = "Bullets to check:\n\n" + "\n\n".join(blocks)

    try:
        result = openai_client.run_structured(
            chat_history=[{"role": "system", "content": system_msg}],
            user_text=user_text,
            json_schema=COHERENCE_SCHEMA,
            schema_name="bullet_coherence",
            model=model,
            temperature=0,
        )
        return result["parsed"].get("results", [])
    except Exception:
        # Graceful degradation: a coherence-check failure must not break the
        # turn — callers treat the absence of a verdict as "coherent".
        return []


# ---------------------------------------------------------------------------
# LLM flow: generate_quicktake
# ---------------------------------------------------------------------------

def generate_quicktake(
    course: Course,
    scope_key: str,
    scope_kind: str,
    scope_week_number: Optional[int] = None,
    scope_survey_ids: Optional[list] = None,
    scope_session_ids: Optional[list] = None,
) -> LEAIQuickTake:
    """Generate (or regenerate) a Quick Take for a course scope.

    Raises:
        ValueError: if fewer than 5 student responses exist in scope.
            (20+ is the recommended threshold for reliable themes.)

    Returns:
        LEAIQuickTake instance (upserted via update_or_create).
    """
    corpus = build_response_corpus(
        course=course,
        scope_kind=scope_kind,
        scope_week_number=scope_week_number,
        scope_survey_ids=scope_survey_ids,
        scope_session_ids=scope_session_ids,
    )

    if len(corpus) < 5:
        raise ValueError(
            f"Insufficient data: need at least 5 responses "
            f"(20+ recommended for reliable themes), "
            f"found {len(corpus)} for scope '{scope_key}'."
        )

    # Build scope label for the user text
    if scope_kind == "week":
        scope_label = f"Week {scope_week_number}"
    elif scope_kind == "custom":
        scope_label = f"Custom scope ({len(corpus)} responses)"
    else:
        scope_label = f"Full course ({course.course_name})"

    system_prompt = default_quicktake_system_prompt()
    chunks = _split_corpus_for_quicktake(corpus, QUICKTAKE_CHUNK_CHAR_LIMIT)

    if len(chunks) == 1:
        user_text = build_quicktake_user_text(
            course_name=course.course_name,
            corpus=corpus,
            scope_label=scope_label,
        )
        result = openai_client.run_structured(
            chat_history=[{"role": "system", "content": system_prompt}],
            user_text=user_text,
            json_schema=QUICKTAKE_SCHEMA,
            schema_name="quicktake",
            model=QUICKTAKE_MODEL,
            temperature=0,
        )
        bullets = result["parsed"].get("bullets", [])
        gaps = result["parsed"].get("gaps", [])
        team_health = result["parsed"].get("team_health", [])
        form_sections = result["parsed"].get("form_sections", [])
        model_name = result.get("model", "")
    else:
        (
            bullets, gaps, team_health, form_sections, model_name,
        ) = _run_chunked_quicktake(
            course_name=course.course_name,
            scope_label=scope_label,
            system_prompt=system_prompt,
            chunks=chunks,
        )
        # Record a compact representation of the chunked prompt for provenance.
        user_text = (
            f"[chunked: {len(chunks)} chunks, {len(corpus)} total responses] "
            f"Course: {course.course_name} | Scope: {scope_label}"
        )

    # Filter hallucinated rids before verification so the verifier doesn't
    # waste cycles on citations that can't possibly resolve, and so the
    # frontend never receives a pill whose popover is "not available."
    corpus_rids = {e["rid"] for e in corpus}
    bullets = filter_bullet_citations(bullets, corpus_rids)

    # Counts BEFORE quote validation, for diagnostic logging.
    raw_quote_count = sum(len(b.get("quotes") or []) for b in bullets)
    raw_gaps = len(gaps or [])
    raw_team_health = len(team_health or [])
    raw_form_sections = len(form_sections or [])

    # Drop any quote whose verbatim span doesn't appear in the cited rid.
    # This is a stronger anti-hallucination guard than rid-existence: the
    # model can't fabricate a sentence and claim a real rid said it.
    bullets, _quote_stats = validate_quote_spans(bullets, corpus)

    # Stop one vivid quote from anchoring every line: a response quoted with
    # the same span on an earlier bullet is dropped from later bullets, so a
    # single student's striking line can't masquerade as the whole class.
    bullets = dedupe_bullet_quotes(bullets)

    # Tag each bullet with its distinct-student support count and flag
    # single-student one-offs. The frontend reads `is_isolated` to split a
    # one-off out of the themes (its own "Mentioned by one student" group)
    # instead of letting an n=1 observation read as a class-wide pattern.
    # Runs after citation filtering so hallucinated rids never inflate a count.
    bullets = annotate_support(bullets)

    # Drop team_health entries that name unknown teams or carry
    # unverifiable quotes.
    team_health = validate_team_health(team_health, corpus)

    # Fold in deterministic per-team response completeness: attach
    # expected/submitted/missing counts to each team and surface EVERY team
    # in scope — including fully-silent ones the model never saw — so the
    # instructor can tell which teams still owe feedback and how much.
    # Counts come from SurveyTeam.size (the roster) vs. distinct submitters
    # in the corpus; they are computed in code, never guessed by the model.
    surveys_qs = _surveys_in_scope(
        course, scope_kind, scope_week_number, scope_survey_ids,
    )
    team_completeness = compute_team_completeness(corpus, surveys_qs)
    if team_completeness:
        team_health = merge_team_completeness(team_health, team_completeness)

    # Drop form_sections entries that name unknown sections or carry
    # unverifiable quotes.
    form_sections = validate_form_sections(form_sections, corpus)

    # Tensions and suggested actions are intentionally no longer generated:
    # the tinted bullets (now carrying their FULL citation count) convey
    # disagreement and how many students are on each side directly, and
    # instructors prefer to pick their own actions. Persist empty lists so a
    # regenerate clears any values a pre-removal row may still hold.
    tensions = []
    actions = []

    final_quote_count = sum(len(b.get("quotes") or []) for b in bullets)
    logger.info(
        "quicktake post-validation: bullets=%d quotes_raw=%d quotes_kept=%d "
        "gaps=%d form_sections_raw=%d form_sections_kept=%d "
        "team_health_raw=%d team_health_final=%d",
        len(bullets), raw_quote_count, final_quote_count,
        raw_gaps,
        raw_form_sections, len(form_sections),
        raw_team_health, len(team_health),
    )

    verification = verify_claims(corpus=corpus, bullets=bullets, model=QUICKTAKE_MODEL)

    # Last line of defense against count-gate evasion. A bullet that lumps two
    # distinct one-offs is phrased so each citation backs its own half, so
    # per-citation verification passes it. A separate coherence pass asks the
    # orthogonal question — do ALL cited responses make the SAME point? — and
    # any multi-citation bullet judged a bundle is split into per-response
    # one-offs (re-indexing `verification`) so a lone voice can't masquerade
    # as a theme.
    coherence = check_bullet_coherence(
        corpus=corpus, bullets=bullets, model=QUICKTAKE_MODEL,
    )
    incoherent = {
        c["bullet_index"] for c in coherence if c.get("coherent") is False
    }
    bullets, verification = enforce_coherence(bullets, incoherent, verification)

    quicktake, _ = LEAIQuickTake.objects.update_or_create(
        course=course,
        scope_key=scope_key,
        defaults={
            "bullets": bullets,
            "tensions": tensions,
            "gaps": gaps,
            "team_health": team_health,
            "form_sections": form_sections,
            "actions": actions,
            "responses_count_at_generation": len(corpus),
            "verification": verification,
            "system_prompt": system_prompt,
            "user_text": user_text,
            "model_name": model_name,
            "status": LEAIQuickTake.STATUS_READY,
            "error": "",
            "job_started_at": None,
        },
    )
    return quicktake


def _is_job_stale(qt: LEAIQuickTake) -> bool:
    """True if a pending/running row has outlived the stale threshold.

    Used to recover from dyno cycles mid-job: the status sits at running
    forever, so on the next generate call we treat it as failed and allow
    a fresh thread to take over.
    """
    if qt.status not in (LEAIQuickTake.STATUS_PENDING, LEAIQuickTake.STATUS_RUNNING):
        return False
    started = qt.job_started_at or qt.updated_at
    if started is None:
        return True
    return (timezone.now() - started).total_seconds() > QUICKTAKE_JOB_STALE_SECONDS


def start_quicktake_job(
    course: Course,
    scope_key: str,
    scope_kind: str,
    scope_week_number: Optional[int] = None,
    scope_survey_ids: Optional[list] = None,
    scope_session_ids: Optional[list] = None,
) -> tuple[LEAIQuickTake, bool]:
    """Mark the row pending and spawn a daemon thread to run generate_quicktake.

    Returns (quicktake, started) where `started` is True if this call kicked
    off a new worker, False if a fresh pending/running job is already in
    flight for the scope (idempotent re-click).

    Raises ValueError if corpus-level preconditions fail (< 5 responses).
    Those errors are surfaced synchronously so the caller can return 400.
    """
    corpus = build_response_corpus(
        course=course,
        scope_kind=scope_kind,
        scope_week_number=scope_week_number,
        scope_survey_ids=scope_survey_ids,
        scope_session_ids=scope_session_ids,
    )
    if len(corpus) < 5:
        raise ValueError(
            f"Insufficient data: need at least 5 responses "
            f"(20+ recommended for reliable themes), "
            f"found {len(corpus)} for scope '{scope_key}'."
        )

    now = timezone.now()
    with transaction.atomic():
        qt, created = LEAIQuickTake.objects.select_for_update().get_or_create(
            course=course,
            scope_key=scope_key,
            defaults={
                "bullets": [],
                "verification": [],
                "system_prompt": "",
                "user_text": "",
                "model_name": "",
                "status": LEAIQuickTake.STATUS_PENDING,
                "error": "",
                "job_started_at": now,
            },
        )
        if not created:
            # Idempotency: if a job is already in flight and not stale,
            # don't double-start. Frontend will poll the existing one.
            if qt.status in (
                LEAIQuickTake.STATUS_PENDING, LEAIQuickTake.STATUS_RUNNING,
            ) and not _is_job_stale(qt):
                return qt, False
            qt.status = LEAIQuickTake.STATUS_PENDING
            qt.error = ""
            qt.job_started_at = now
            qt.save(update_fields=["status", "error", "job_started_at", "updated_at"])

    def _worker(qt_pk: int, course_pk: int) -> None:
        # Each thread gets its own Django DB connection; close at end
        # to avoid leaking connections across worker lifetimes.
        try:
            course_obj = Course.objects.get(pk=course_pk)
            LEAIQuickTake.objects.filter(pk=qt_pk).update(
                status=LEAIQuickTake.STATUS_RUNNING,
                updated_at=timezone.now(),
            )
            generate_quicktake(
                course=course_obj,
                scope_key=scope_key,
                scope_kind=scope_kind,
                scope_week_number=scope_week_number,
                scope_survey_ids=scope_survey_ids,
                scope_session_ids=scope_session_ids,
            )
        except ValueError as e:
            LEAIQuickTake.objects.filter(pk=qt_pk).update(
                status=LEAIQuickTake.STATUS_FAILED,
                error=str(e),
            )
        except openai_client.OpenAIRefusalError as e:
            LEAIQuickTake.objects.filter(pk=qt_pk).update(
                status=LEAIQuickTake.STATUS_FAILED,
                error=getattr(e, "detail", str(e)),
            )
        except openai_client.OpenAIClientError as e:
            LEAIQuickTake.objects.filter(pk=qt_pk).update(
                status=LEAIQuickTake.STATUS_FAILED,
                error=getattr(e, "detail", str(e)),
            )
        except Exception as e:
            logger.exception("quicktake worker crashed for qt=%s", qt_pk)
            LEAIQuickTake.objects.filter(pk=qt_pk).update(
                status=LEAIQuickTake.STATUS_FAILED,
                error=f"Internal error: {type(e).__name__}",
            )
        finally:
            connection.close()

    thread = threading.Thread(
        target=_worker,
        args=(qt.pk, course.pk),
        name=f"quicktake-{qt.pk}",
        daemon=True,
    )
    thread.start()
    return qt, True


def _split_corpus_for_quicktake(
    corpus: list[dict],
    char_limit: int,
) -> list[list[dict]]:
    """Greedy-pack the corpus into chunks under `char_limit` characters.

    R-ids stay globally unique across chunks because they were assigned once
    in build_response_corpus, so citations remain coherent after merging.
    A single response larger than the limit gets its own chunk.
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for entry in corpus:
        entry_chars = len(entry.get("text", "")) + len(entry.get("rid", "")) + 4
        if current and current_chars + entry_chars > char_limit:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(entry)
        current_chars += entry_chars
    if current:
        chunks.append(current)
    return chunks


def _run_chunked_quicktake(
    course_name: str,
    scope_label: str,
    system_prompt: str,
    chunks: list[list[dict]],
) -> tuple[
    list[dict], list[dict], list[dict], list[dict], str,
]:
    """Map-reduce: summarise each chunk, then merge bullets via a reducer call.

    Chunks run in parallel to stay under Heroku's 30s request window. The
    reducer is a structured call that dedupes/consolidates bullets while
    preserving the original global R-id citations.

    Returns:
        (bullets, gaps, team_health, form_sections, model_name)

    The auxiliary fields (gaps/team_health/form_sections) are union-with-dedup
    across chunks rather than reducer-merged — each chunk independently
    surfaces section/team/gap insights and we keep the first occurrence of
    each unique topic/team/title. (Tensions and suggested actions are no
    longer generated, so they are not collected here.)
    """
    total_chunks = len(chunks)

    def summarise_chunk(idx_chunk: tuple[int, list[dict]]) -> dict:
        idx, chunk = idx_chunk
        chunk_label = f"{scope_label} — part {idx + 1} of {total_chunks}"
        user_text = build_quicktake_user_text(
            course_name=course_name,
            corpus=chunk,
            scope_label=chunk_label,
        )
        return openai_client.run_structured(
            chat_history=[{"role": "system", "content": system_prompt}],
            user_text=user_text,
            json_schema=QUICKTAKE_SCHEMA,
            schema_name="quicktake",
            model=QUICKTAKE_MODEL,
            temperature=0,
        )

    max_workers = min(QUICKTAKE_CHUNK_MAX_WORKERS, total_chunks)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        chunk_results = list(pool.map(summarise_chunk, list(enumerate(chunks))))

    partial_bullets: list[dict] = []
    # Phase 5/7/8: collect auxiliary fields from each chunk's response.
    # Dedup by a stable key per field type so we don't surface the same
    # section/team/gap N times for an N-chunk corpus.
    seen_gap_topics: set[str] = set()
    seen_team_names: set[str] = set()
    seen_section_titles: set[str] = set()
    agg_gaps: list[dict] = []
    agg_team_health: list[dict] = []
    agg_form_sections: list[dict] = []

    for res in chunk_results:
        parsed = res.get("parsed", {}) or {}
        partial_bullets.extend(parsed.get("bullets", []))
        for g in parsed.get("gaps") or []:
            key = (g.get("topic") or "").strip().lower()
            if key and key not in seen_gap_topics:
                seen_gap_topics.add(key)
                agg_gaps.append(g)
        for th in parsed.get("team_health") or []:
            key = (th.get("team_name") or "").strip()
            if key and key not in seen_team_names:
                seen_team_names.add(key)
                agg_team_health.append(th)
        for fs in parsed.get("form_sections") or []:
            key = (fs.get("section_title") or "").strip().lower()
            if key and key not in seen_section_titles:
                seen_section_titles.add(key)
                agg_form_sections.append(fs)

    model_name = chunk_results[0].get("model", "") if chunk_results else ""

    if not partial_bullets:
        return (
            [], agg_gaps, agg_team_health, agg_form_sections, model_name,
        )

    # Reduce step: ask the model to consolidate overlapping bullets while
    # preserving R-id citations. If reduction fails, fall back to concatenation.
    reducer_system = (
        "You are merging bullet-point summaries produced from different chunks "
        "of the same student-feedback corpus into a single concise set of "
        "bullets. Preserve the exact [R<n>] citation IDs from the input — do "
        "not invent new ones. Merge overlapping themes, drop duplicates, and "
        "keep the bullets objective and specific. When you merge two bullets "
        "about the same theme, the merged bullet must carry the UNION of all "
        "their [R<n>] citations — never drop a citation, since the count of "
        "distinct citations is how many students back the theme. Each merged "
        "bullet must "
        "also carry a `sentiment` tag (`positive`, `negative`, `mixed`, or "
        "`neutral`). Inputs are pre-tagged with their source sentiment — use "
        "those as the primary signal; if two merged partials disagree, "
        "prefer `mixed`. Do not default to `neutral` when a real positive "
        "or negative signal is present."
    )
    lines = [
        f"Course: {course_name}",
        f"Scope: {scope_label}",
        f"Total chunks: {total_chunks}",
        "",
        "--- Partial bullets (each line shows [sentiment] text [R-ids]) ---",
    ]
    for b in partial_bullets:
        cited = "".join(f"[{rid}]" for rid in b.get("cited_ids", []))
        sent = (b.get("sentiment") or "neutral").strip() or "neutral"
        lines.append(f"- [{sent}] {b.get('text', '')} {cited}".rstrip())
    lines.append("")
    lines.append(
        "Produce a merged set of bullets. Each bullet must cite the supporting "
        "response IDs inline using [R<n>] notation drawn only from the IDs "
        "above, and must include a `sentiment` value carried from (or "
        "reconciled across) the merged partials."
    )
    reducer_user = "\n".join(lines)

    # Carry quotes through the reducer step. The reducer is asked for
    # text + cited_ids only; we don't trust it to faithfully re-emit
    # verbatim quotes through a merge. Instead, we build a rid → quote
    # map from the original per-chunk bullets and reattach quotes to the
    # reducer's merged bullets by their cited_ids.
    quote_by_rid: dict[str, dict] = {}
    for b in partial_bullets:
        for q in b.get("quotes") or []:
            rid = q.get("rid", "")
            if rid and rid not in quote_by_rid and q.get("text"):
                quote_by_rid[rid] = q

    def attach_quotes(bs: list[dict]) -> list[dict]:
        out = []
        for b in bs:
            quotes = []
            seen = set()
            for rid in b.get("cited_ids", []):
                q = quote_by_rid.get(rid)
                if q and rid not in seen:
                    quotes.append(q)
                    seen.add(rid)
            out.append({**b, "quotes": quotes})
        return out

    try:
        reduced = openai_client.run_structured(
            chat_history=[{"role": "system", "content": reducer_system}],
            user_text=reducer_user,
            json_schema=QUICKTAKE_SCHEMA,
            schema_name="quicktake",
            model=QUICKTAKE_MODEL,
            temperature=0,
        )
        bullets = reduced["parsed"].get("bullets", [])
        model_name = reduced.get("model", model_name)
        if bullets:
            return (
                attach_quotes(bullets), agg_gaps, agg_team_health,
                agg_form_sections, model_name,
            )
    except Exception:
        pass

    return (
        attach_quotes(partial_bullets), agg_gaps, agg_team_health,
        agg_form_sections, model_name,
    )


# ---------------------------------------------------------------------------
# LLM flow: chat turn (async)
# ---------------------------------------------------------------------------

# A chat turn that stays in pending/running past this many seconds is
# treated as a dyno-cycle zombie by the polling endpoints, which flip it
# to failed so the UI can retry instead of spinning forever.
CHAT_TURN_JOB_STALE_SECONDS = 180


def _is_chat_message_stale(msg: LEAIChatMessage) -> bool:
    """True if a pending/running assistant row has outlived the stale threshold."""
    if msg.status not in (
        LEAIChatMessage.STATUS_PENDING, LEAIChatMessage.STATUS_RUNNING,
    ):
        return False
    started = msg.job_started_at or msg.created_at
    if started is None:
        return True
    return (timezone.now() - started).total_seconds() > CHAT_TURN_JOB_STALE_SECONDS


def _generate_assistant_response(
    session: LEAIChatSession,
    user_text: str,
    *,
    exclude_message_pks: Optional[list] = None,
) -> tuple[str, list]:
    """Pure LLM body of a chat turn: build context, call the model + verifier,
    return ``(cleaned_text, cited)``. Does NOT write to the database.

    ``exclude_message_pks`` is the set of message rows to omit from the
    rebuilt chat history — typically the just-saved user message (and, in
    the async path, the pending assistant placeholder). Prior messages
    with status != ready are skipped automatically so failed turns do not
    contaminate future turns.

    Raises:
        OpenAI* errors from openai_client on LLM failure.
    """
    course = session.course
    corpus = build_response_corpus(
        course=course,
        scope_kind=session.scope_kind,
        scope_week_number=session.scope_week_number,
        scope_survey_ids=list(session.scope_survey_ids or []),
        scope_session_ids=list(session.scope_session_ids or []),
    )

    base_system = (
        session.system_prompt_override
        if session.system_prompt_override
        else default_chat_system_prompt(corpus)
    )
    corpus_block = build_chat_corpus_block(corpus)
    full_system = f"{base_system}\n\n{corpus_block}"

    prior_qs = session.messages.filter(status=LEAIChatMessage.STATUS_READY)
    if exclude_message_pks:
        prior_qs = prior_qs.exclude(pk__in=exclude_message_pks)
    prior_messages = prior_qs.order_by("created_at")

    chat_history = [{"role": "system", "content": full_system}]
    for msg in prior_messages:
        if msg.role in ("user", "assistant"):
            chat_history.append({"role": msg.role, "content": msg.text})

    # Structured output (Phase 6): the model returns { answer, quotes }
    # where quotes carries one verbatim span per cited rid for the
    # popover surface.
    result = openai_client.run_structured(
        chat_history=chat_history,
        user_text=user_text,
        json_schema=CHAT_TURN_SCHEMA,
        schema_name="chat_turn",
    )
    parsed = result.get("parsed") or {}
    raw_answer = parsed.get("answer", "")
    raw_quotes = parsed.get("quotes", []) or []

    # Strip rids the model invented (not in corpus); otherwise the
    # frontend renders pills whose popovers can't resolve.
    valid_rids = {e["rid"] for e in corpus}
    cleaned_text, cited_rids = parse_inline_citations(raw_answer, valid_rids)

    # Verify each quote against the cited rid's source. A verified quote
    # becomes the popover content; unverified ones leave quote_text empty
    # so the popover falls back to the full session text.
    corpus_by_rid_norm = {
        e["rid"]: _normalize_for_quote_match(e["text"]) for e in corpus
    }
    quote_text_by_rid: dict[str, str] = {}
    for q in raw_quotes:
        rid = q.get("rid", "")
        qtext = q.get("text", "")
        if not rid or not qtext or rid not in valid_rids:
            continue
        source = corpus_by_rid_norm.get(rid, "")
        if _normalize_for_quote_match(qtext) in source:
            quote_text_by_rid.setdefault(rid, qtext)

    cited = []
    for i, rid in enumerate(cited_rids, 1):
        cited.append({
            'rid': rid,
            'pill_index': i,
            'verdict': None,
            'quote_text': quote_text_by_rid.get(rid, ''),
        })

    if cited:
        pseudo_bullets = [{"text": cleaned_text, "cited_ids": cited_rids}]
        try:
            verification = verify_claims(corpus=corpus, bullets=pseudo_bullets)
            verdict_map = {v['source_id']: v['verdict'] for v in verification}
            for c in cited:
                c['verdict'] = verdict_map.get(c['rid'])
        except Exception:
            pass  # leave verdicts as None

    return cleaned_text, cited


def run_chat_turn(
    session: LEAIChatSession,
    user_text: str,
) -> LEAIChatMessage:
    """Synchronous chat turn (legacy entry point).

    Saves user + assistant messages atomically so an LLM failure rolls
    back the user message. Kept for unit tests and any direct in-process
    caller; the HTTP turn endpoint uses ``start_chat_turn_job`` instead so
    the LLM work happens off the request thread (Heroku's 30s router
    timeout makes a synchronous response unsafe for multi-survey scopes).
    """
    with transaction.atomic():
        user_msg = LEAIChatMessage.objects.create(
            session=session,
            role="user",
            text=user_text,
            cited=[],
        )
        cleaned_text, cited = _generate_assistant_response(
            session, user_text, exclude_message_pks=[user_msg.pk],
        )
        assistant_msg = LEAIChatMessage.objects.create(
            session=session,
            role="assistant",
            text=cleaned_text,
            cited=cited,
        )
    return assistant_msg


def start_chat_turn_job(
    session: LEAIChatSession,
    user_text: str,
) -> tuple[LEAIChatMessage, LEAIChatMessage]:
    """Async chat turn: save user msg + pending assistant placeholder, spawn
    a worker thread, return both rows immediately.

    The HTTP request returns 202 with the placeholder; the frontend polls
    ``GET /api/leai_chat_sessions/<sid>/messages/<mid>/`` until status
    flips to ready or failed. This keeps the request well under Heroku's
    30s router timeout regardless of how many surveys are in scope.

    Raises:
        ValueError on empty user_text.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        raise ValueError("user_text is required")

    now = timezone.now()
    with transaction.atomic():
        user_msg = LEAIChatMessage.objects.create(
            session=session,
            role="user",
            text=user_text,
            cited=[],
            status=LEAIChatMessage.STATUS_READY,
        )
        assistant_msg = LEAIChatMessage.objects.create(
            session=session,
            role="assistant",
            text="",
            cited=[],
            status=LEAIChatMessage.STATUS_PENDING,
            job_started_at=now,
        )

    def _worker(session_pk, user_msg_pk, assistant_msg_pk, user_text_local):
        try:
            sess = LEAIChatSession.objects.get(pk=session_pk)
            LEAIChatMessage.objects.filter(pk=assistant_msg_pk).update(
                status=LEAIChatMessage.STATUS_RUNNING,
            )
            cleaned_text, cited = _generate_assistant_response(
                sess,
                user_text_local,
                exclude_message_pks=[user_msg_pk, assistant_msg_pk],
            )
            LEAIChatMessage.objects.filter(pk=assistant_msg_pk).update(
                text=cleaned_text,
                cited=cited,
                status=LEAIChatMessage.STATUS_READY,
                error="",
            )
        except ValueError as e:
            LEAIChatMessage.objects.filter(pk=assistant_msg_pk).update(
                status=LEAIChatMessage.STATUS_FAILED, error=str(e),
            )
        except openai_client.OpenAIRefusalError as e:
            LEAIChatMessage.objects.filter(pk=assistant_msg_pk).update(
                status=LEAIChatMessage.STATUS_FAILED,
                error=getattr(e, "detail", str(e)),
            )
        except openai_client.OpenAIClientError as e:
            LEAIChatMessage.objects.filter(pk=assistant_msg_pk).update(
                status=LEAIChatMessage.STATUS_FAILED,
                error=getattr(e, "detail", str(e)),
            )
        except Exception as e:
            logger.exception("chat turn worker crashed for msg=%s", assistant_msg_pk)
            LEAIChatMessage.objects.filter(pk=assistant_msg_pk).update(
                status=LEAIChatMessage.STATUS_FAILED,
                error=f"Internal error: {type(e).__name__}",
            )
        finally:
            # Close this thread's DB connection so long-running daemons
            # don't leak — but skip when the worker is running on the
            # main thread (unit tests using an inline Thread shim);
            # closing the test's connection would break subsequent
            # assertions on the same TestCase.
            if threading.current_thread() is not threading.main_thread():
                connection.close()

    thread = threading.Thread(
        target=_worker,
        args=(session.pk, user_msg.pk, assistant_msg.pk, user_text),
        name=f"chat-turn-{assistant_msg.pk}",
        daemon=True,
    )
    thread.start()
    return user_msg, assistant_msg
