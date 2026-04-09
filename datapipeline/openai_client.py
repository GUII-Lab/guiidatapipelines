"""OpenAI Responses API client plumbing for the datapipeline Django app.

All SDK use lives in this module. Views never `import openai` directly — they
call `run_chat` / `run_structured` and catch `OpenAIClientError` subclasses.

The module is intentionally free of Django imports so its helpers can be
unit-tested and imported from the standalone dry-run script.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import openai
from openai import OpenAI


DEFAULT_MODEL: str = os.environ.get("OPENAI_DEFAULT_MODEL", "gpt-5.1")


class OpenAIClientError(Exception):
    """Base error raised by this module. Carries an HTTP status hint
    that views should use when translating to JsonResponse status codes."""

    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class OpenAIRefusalError(OpenAIClientError):
    """Raised when the model refuses or returns unparseable JSON under
    strict-schema mode. Views should map this to HTTP 422."""

    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail, status_code=status_code)


class OpenAIConfigError(OpenAIClientError):
    """Raised when the proxy itself is misconfigured (missing key,
    auth failure). Views should map this to HTTP 500."""

    def __init__(self, detail: str, status_code: int = 500):
        super().__init__(detail, status_code=status_code)


# --- public API ---------------------------------------------------------
# The following functions are filled in by subsequent tasks:
#   get_client()
#   build_responses_input()
#   translate_usage()
#   run_chat()
#   run_structured()


def build_responses_input(
    chat_history: Optional[list],
    user_text: str,
) -> tuple[Optional[str], list[dict]]:
    """Split chat_history into (instructions, input_messages) suitable for
    client.responses.create(instructions=..., input=...).

    - `role: "system"` (or legacy sent_by='system') messages are pulled out
      and newline-joined into a single `instructions` string.
    - Remaining user/assistant messages map to {'role', 'content'} dicts.
    - user_text is appended as the final {'role': 'user'} turn.
    - Legacy normalization: sent_by 'student' → user, 'gpt'/'ai' → assistant.
    - Legacy content fallback: msg['text'] is used when msg['content'] absent.
    - Malformed entries (non-dict, missing content, empty content) are
      dropped silently to match current view behavior.

    Returns (instructions_or_none, input_messages_list). instructions is None
    when no system messages were present."""
    instructions_parts: list[str] = []
    input_messages: list[dict] = []

    for msg in (chat_history or []):
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "user")
        sent_by_raw = msg.get("sent_by")
        if sent_by_raw:
            sent_by = str(sent_by_raw).lower()
            if sent_by in ("user", "student"):
                role = "user"
            elif sent_by in ("assistant", "gpt", "ai"):
                role = "assistant"
            elif sent_by == "system":
                role = "system"

        content = msg.get("content") or msg.get("text") or ""
        if not content:
            continue

        if role == "system":
            instructions_parts.append(content)
        else:
            input_messages.append({"role": role, "content": content})

    input_messages.append({"role": "user", "content": user_text})

    instructions = "\n\n".join(instructions_parts) if instructions_parts else None
    return instructions, input_messages


def translate_usage(usage: Any) -> dict:
    """Map Responses API usage (input_tokens/output_tokens/total_tokens) to
    the Chat Completions shape (prompt_tokens/completion_tokens/total_tokens)
    so the /api/openai-chat/ frontend contract stays byte-identical.

    Accepts either a pydantic-style object with attributes OR a plain dict.
    Returns {} if usage is None."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    return {
        "prompt_tokens": getattr(usage, "input_tokens", 0),
        "completion_tokens": getattr(usage, "output_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
    }


# Lazily-constructed singleton. Reset in tests via _reset_client_for_tests().
_client: Optional[OpenAI] = None


def get_client() -> OpenAI:
    """Return the lazily-constructed OpenAI client singleton.

    Reads the API key from the `oaiKey` env var (matching the name the
    existing view uses). Raises OpenAIConfigError if unset."""
    global _client
    if _client is None:
        api_key = os.environ.get("oaiKey")
        if not api_key:
            raise OpenAIConfigError(
                "oaiKey environment variable is not set",
            )
        _client = OpenAI(
            api_key=api_key,
            timeout=60.0,
            max_retries=2,
        )
    return _client


def _reset_client_for_tests() -> None:
    """Clear the cached client. For unit-test use only."""
    global _client
    _client = None
