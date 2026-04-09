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
