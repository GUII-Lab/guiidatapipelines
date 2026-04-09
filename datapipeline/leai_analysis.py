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

import re
from typing import Optional

from django.db import transaction

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
        "(Learning Experience AI). "
        "You will be given a corpus of student responses, each identified by a "
        "response ID (e.g. R1, R2, ...). "
        "Your task is to synthesise the key themes, concerns, and insights into "
        "a concise set of bullet points. "
        "Each bullet must cite the specific response IDs that support it using "
        "inline notation such as [R17] or [R3][R12]. "
        "Be objective, accurate, and avoid over-generalisation. "
        "Only reference response IDs that genuinely support the bullet."
    )


def default_chat_system_prompt() -> str:
    """Return the default system prompt used for Feedback Chat turns."""
    return (
        "You are LEAI (Learning Experience AI), an educational analytics "
        "assistant that helps instructors explore and understand anonymous "
        "student feedback. "
        "You have access to a corpus of student responses shown below. "
        "When making claims, cite response IDs inline using square-bracket "
        "notation like [R17] or [R3]. Always use this exact format — each "
        "citation must be a separate [R<number>] tag, never bold, never "
        "comma-separated inside brackets. "
        "Be thoughtful, evidence-based, and pedagogically sensitive. "
        "Do not reveal individual student identities — all responses are "
        "anonymous."
    )


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
                },
                "required": ["text", "cited_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["bullets"],
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


# ---------------------------------------------------------------------------
# Corpus builder
# ---------------------------------------------------------------------------

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

    Scope rules:
        "course"  — all FeedbackGPT surveys for this course
        "week"    — only surveys where week_number == scope_week_number
        "custom"  — surveys listed in scope_survey_ids OR sessions in scope_session_ids
    """
    # 1. Resolve the set of FeedbackGPT surveys in scope
    surveys_qs = FeedbackGPT.objects.filter(course=course)

    if scope_kind == "week":
        surveys_qs = surveys_qs.filter(week_number=scope_week_number)
    elif scope_kind == "custom":
        if scope_survey_ids:
            surveys_qs = surveys_qs.filter(pk__in=scope_survey_ids)
        else:
            surveys_qs = surveys_qs.none()

    survey_map: dict[int, Optional[int]] = {
        s.pk: s.week_number for s in surveys_qs
    }
    survey_ids = list(survey_map.keys())

    # 2. Fetch student messages (exclude AI turns)
    messages_qs = (
        FeedbackMessage.objects
        .filter(gpt_id__in=survey_ids, sent_by="user-message")
        .order_by("session_id", "created_at")
    )

    if scope_kind == "custom" and scope_session_ids:
        messages_qs = messages_qs.filter(session_id__in=scope_session_ids)

    # 3. Group by session_id, preserving the gpt_id for each session
    sessions: dict[str, dict] = {}  # session_id → {gpt_id, texts}
    for msg in messages_qs:
        sid = msg.session_id
        if sid not in sessions:
            sessions[sid] = {"gpt_id": msg.gpt_id, "texts": []}
        sessions[sid]["texts"].append(msg.content)

    # 4. Sort deterministically: week_number ASC (None last), then session_id lexical ASC
    def sort_key(item):
        sid, data = item
        week = survey_map.get(data["gpt_id"])
        return (week is None, week or 0, sid)

    sorted_sessions = sorted(sessions.items(), key=sort_key)

    # 5. Build corpus entries with R-IDs
    corpus = []
    for idx, (sid, data) in enumerate(sorted_sessions, start=1):
        gpt_id = data["gpt_id"]
        corpus.append({
            "rid": f"R{idx}",
            "survey_id": gpt_id,
            "session_id": sid,
            "week_number": survey_map.get(gpt_id),
            "text": " | ".join(data["texts"]),
        })

    return corpus


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_quicktake_user_text(
    course_name: str,
    corpus: list[dict],
    scope_label: str,
) -> str:
    """Build the user-turn text for a Quick Take structured call."""
    lines = [
        f"Course: {course_name}",
        f"Scope: {scope_label}",
        f"Total responses: {len(corpus)}",
        "",
        "--- Student Responses ---",
    ]
    for entry in corpus:
        lines.append(f"[{entry['rid']}] {entry['text']}")
    lines.append("")
    lines.append(
        "Please synthesise these responses into bullet points, "
        "citing response IDs inline."
    )
    return "\n".join(lines)


def build_chat_corpus_block(corpus: list[dict]) -> str:
    """Build the numbered response block appended to the chat system prompt."""
    lines = ["--- Student Response Corpus ---"]
    for entry in corpus:
        lines.append(f"[{entry['rid']}] {entry['text']}")
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


def parse_inline_citations(text: str) -> tuple[str, list[str]]:
    """Replace [R<n>] citations with sequential [1], [2], ... indices.

    Returns:
        (cleaned_text, cited_list)

    Each occurrence of [R<n>] in `text` is replaced with a 1-based sequential
    index.  The same R-id appearing multiple times gets separate sequential
    pill indices (i.e. duplicate R-ids are NOT collapsed).

    Also handles comma-separated citations like [R5, R25, R35] by first
    expanding them into individual [R5][R25][R35] format.

    cited_list contains the original R-ids in the order they first appear in
    the text (de-duplicated for the list, but pills are still sequential).
    """
    # Normalize variant citation formats first
    text = _normalize_citations(text)

    cited_order: list[str] = []  # ordered unique R-ids
    seen: set[str] = set()
    pill_counter = 0

    def replace(match: re.Match) -> str:
        nonlocal pill_counter
        rid = f"R{match.group(1)}"
        pill_counter += 1
        if rid not in seen:
            seen.add(rid)
            cited_order.append(rid)
        return f"[{pill_counter}]"

    cleaned = _CITATION_RE.sub(replace, text)
    return cleaned, cited_order


# ---------------------------------------------------------------------------
# LLM flow: verify_claims
# ---------------------------------------------------------------------------

def verify_claims(
    corpus: list[dict],
    bullets: list[dict],
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
        "For each bullet point, check whether each cited response ID actually "
        "supports the claim. "
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
            temperature=0,
        )
        return result["parsed"].get("results", [])
    except Exception:
        # Graceful degradation: verification failure must not break the turn
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
        ValueError: if fewer than 20 student responses exist in scope.

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

    if len(corpus) < 20:
        raise ValueError(
            f"Insufficient data: need at least 20 responses, "
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
        temperature=0,
    )

    bullets = result["parsed"].get("bullets", [])
    verification = verify_claims(corpus=corpus, bullets=bullets)

    quicktake, _ = LEAIQuickTake.objects.update_or_create(
        course=course,
        scope_key=scope_key,
        defaults={
            "bullets": bullets,
            "verification": verification,
            "system_prompt": system_prompt,
            "user_text": user_text,
            "model_name": result.get("model", ""),
        },
    )
    return quicktake


# ---------------------------------------------------------------------------
# LLM flow: run_chat_turn
# ---------------------------------------------------------------------------

def run_chat_turn(
    session: LEAIChatSession,
    user_text: str,
) -> LEAIChatMessage:
    """Execute one chat turn for a Feedback Chat session.

    Saves the user message, calls the LLM, parses citations, runs
    verify_claims, saves the assistant message.

    The entire operation (save user + save assistant) is wrapped in
    transaction.atomic() so that an LLM failure rolls back the user message.

    Returns:
        The saved LEAIChatMessage for the assistant turn.
    """
    course = session.course

    # Build corpus once (outside the transaction — read-only)
    corpus = build_response_corpus(
        course=course,
        scope_kind=session.scope_kind,
        scope_week_number=session.scope_week_number,
        scope_survey_ids=list(session.scope_survey_ids or []),
        scope_session_ids=list(session.scope_session_ids or []),
    )

    # Build system prompt (allow override)
    base_system = (
        session.system_prompt_override
        if session.system_prompt_override
        else default_chat_system_prompt()
    )
    corpus_block = build_chat_corpus_block(corpus)
    full_system = f"{base_system}\n\n{corpus_block}"

    with transaction.atomic():
        # Save user message
        user_msg = LEAIChatMessage.objects.create(
            session=session,
            role="user",
            text=user_text,
            cited=[],
        )

        # Build chat history from prior messages (excluding the one just saved)
        prior_messages = (
            session.messages
            .exclude(pk=user_msg.pk)
            .order_by("created_at")
        )
        chat_history = [{"role": "system", "content": full_system}]
        for msg in prior_messages:
            if msg.role in ("user", "assistant"):
                chat_history.append({"role": msg.role, "content": msg.text})

        # Call LLM
        result = openai_client.run_chat(
            chat_history=chat_history,
            user_text=user_text,
        )

        raw_response = result["response"]

        # Parse citations
        cleaned_text, cited_rids = parse_inline_citations(raw_response)

        # Build full cited array with pill indices
        cited = []
        for i, rid in enumerate(cited_rids, 1):
            cited.append({
                'rid': rid,
                'pill_index': i,
                'verdict': None,
            })

        # Verify citations (graceful — never raises)
        if cited:
            pseudo_bullets = [{"text": cleaned_text, "cited_ids": cited_rids}]
            try:
                verification = verify_claims(corpus=corpus, bullets=pseudo_bullets)
                verdict_map = {v['source_id']: v['verdict'] for v in verification}
                for c in cited:
                    c['verdict'] = verdict_map.get(c['rid'])
            except Exception:
                pass  # leave verdicts as None

        # Save assistant message
        assistant_msg = LEAIChatMessage.objects.create(
            session=session,
            role="assistant",
            text=cleaned_text,
            cited=cited,
        )

    return assistant_msg
