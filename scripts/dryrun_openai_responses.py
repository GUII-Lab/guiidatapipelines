#!/usr/bin/env python3
"""Standalone dry-run script for the openai_chat / openai_structured endpoints.

Hits the real endpoints — and therefore the real OpenAI API — with canned
payloads and asserts the response shape matches what the frontend expects.

Usage:
    python scripts/dryrun_openai_responses.py \
        --base-url https://guiidata-b6c968e6ed85.herokuapp.com/datapipeline/api

    # Or, against a local dev server:
    python scripts/dryrun_openai_responses.py \
        --base-url http://localhost:8000/datapipeline/api

Exits 0 on full success, 1 if any check fails.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any, Callable

import requests


def _header(label: str) -> None:
    print(f"\n=== {label} ===")


def _check(label: str, cond: bool, detail: str = "") -> bool:
    marker = "PASS" if cond else "FAIL"
    line = f"  [{marker}] {label}"
    if detail and not cond:
        line += f" -- {detail}"
    print(line)
    return cond


def _post(base_url: str, path: str, body: dict) -> requests.Response:
    url = base_url.rstrip("/") + path
    return requests.post(url, json=body, timeout=90)


def check_chat_happy_path(base_url: str) -> bool:
    _header("chat: happy path (empty history, simple prompt)")
    resp = _post(base_url, "/openai-chat/", {
        "chat_history": [],
        "user_text": "Reply with the single word 'pong' and nothing else.",
    })
    ok = True
    ok &= _check("status code is 200", resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        return False
    body = resp.json()
    ok &= _check("body.status == 'success'", body.get("status") == "success")
    ok &= _check("body.response is non-empty string",
                 isinstance(body.get("response"), str) and len(body["response"]) > 0)
    usage = body.get("usage", {})
    ok &= _check("body.usage has prompt_tokens", "prompt_tokens" in usage)
    ok &= _check("body.usage has completion_tokens", "completion_tokens" in usage)
    ok &= _check("body.usage has total_tokens", "total_tokens" in usage)
    return ok


def check_chat_with_system_message_and_model(base_url: str) -> bool:
    _header("chat: system message + multi-turn history + explicit model")
    resp = _post(base_url, "/openai-chat/", {
        "chat_history": [
            {"role": "system", "content": "You are a terse assistant. Respond in under 10 words."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello."},
        ],
        "user_text": "What is 2 + 2?",
        "model": "gpt-5.1",
    })
    ok = True
    ok &= _check("status code is 200", resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        return False
    body = resp.json()
    ok &= _check("body.status == 'success'", body.get("status") == "success")
    ok &= _check("body.response is non-empty string",
                 isinstance(body.get("response"), str) and len(body["response"]) > 0)
    return ok


def check_chat_missing_user_text(base_url: str) -> bool:
    _header("chat: missing user_text returns 400")
    resp = _post(base_url, "/openai-chat/", {"chat_history": []})
    return _check("status code is 400", resp.status_code == 400, f"got {resp.status_code}")


def check_structured_happy_path(base_url: str) -> bool:
    _header("structured: sentiment schema happy path")
    schema = {
        "type": "object",
        "properties": {
            "sentiment": {
                "type": "string",
                "enum": ["positive", "negative", "neutral"],
            },
        },
        "required": ["sentiment"],
        "additionalProperties": False,
    }
    resp = _post(base_url, "/openai-structured/", {
        "chat_history": [],
        "user_text": "The course was great. I learned so much!",
        "json_schema": schema,
        "schema_name": "sentiment_analysis",
    })
    ok = True
    ok &= _check("status code is 200", resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        return False
    body = resp.json()
    ok &= _check("body.status == 'success'", body.get("status") == "success")
    ok &= _check("body.response is a JSON-parseable string",
                 isinstance(body.get("response"), str)
                 and _safe_json_loads(body["response"]) is not None)
    parsed = body.get("parsed")
    ok &= _check("body.parsed is a dict", isinstance(parsed, dict))
    if isinstance(parsed, dict):
        sentiment = parsed.get("sentiment")
        ok &= _check("parsed.sentiment in enum",
                     sentiment in ("positive", "negative", "neutral"),
                     f"got {sentiment!r}")
    ok &= _check("body.response json-matches body.parsed",
                 _safe_json_loads(body.get("response", "")) == body.get("parsed"))
    return ok


def check_structured_missing_schema(base_url: str) -> bool:
    _header("structured: missing json_schema returns 400")
    resp = _post(base_url, "/openai-structured/", {
        "user_text": "hi",
    })
    return _check("status code is 400", resp.status_code == 400, f"got {resp.status_code}")


def _safe_json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        required=True,
        help="Base URL including /datapipeline/api (no trailing slash).",
    )
    parser.add_argument(
        "--confirm-real-api",
        action="store_true",
        required=True,
        help=(
            "Required acknowledgment that this script makes real OpenAI API "
            "calls against the live backend and will cost real money on the "
            "configured OpenAI key. Pass this flag explicitly every time."
        ),
    )
    args = parser.parse_args()

    print(f"Running dry-run checks against: {args.base_url}")

    checks: list[Callable[[str], bool]] = [
        check_chat_happy_path,
        check_chat_with_system_message_and_model,
        check_chat_missing_user_text,
        check_structured_happy_path,
        check_structured_missing_schema,
    ]

    results = []
    for check in checks:
        try:
            results.append(check(args.base_url))
        except Exception:
            print(f"  [FAIL] {check.__name__} raised an exception:")
            traceback.print_exc()
            results.append(False)

    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
