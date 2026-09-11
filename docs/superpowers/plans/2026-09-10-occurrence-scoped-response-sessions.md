# Occurrence-Scoped Response Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach every newly API-persisted student chat message to one authoritative, survey-occurrence-scoped anonymous response session without rewriting historical data.

**Architecture:** Add a schema-only normalized parent and nullable message relationship, then route the existing single and bulk response endpoints through one transaction-owning service. Preserve legacy fields and reads until a later backfill/parity/cutover milestone.

**Tech Stack:** Django 4.2, PostgreSQL, Django TestCase/Client

**Spec:** `docs/superpowers/specs/2026-09-10-occurrence-scoped-response-sessions-design.md`

## Global Constraints

- No production access, deploy, push, data backfill, or destructive cleanup.
- Students remain anonymous; do not add accounts, roster links, device identifiers, or behavioral telemetry.
- Preserve existing `FeedbackMessage` fields and endpoint request shapes.
- New schema fields are nullable where required for untouched historical rows.
- Every production behavior is introduced only after its test fails for the expected missing-feature reason.

---

### Task 1: Add the normalized response-session schema

**Files:**
- Modify: `datapipeline/models.py`
- Modify: `datapipeline/admin.py`
- Create: `datapipeline/migrations/0047_responsesession_feedbackmessage_sequence_and_more.py`
- Create: `datapipeline/tests/test_response_sessions.py`

**Interfaces:**
- Produces: `ResponseSession`; `FeedbackMessage.response_session`; `FeedbackMessage.sequence`.
- Consumes: existing `Course`, `FeedbackGPT`, and `FeedbackMessage` models.

- [x] Write model tests proving survey-scoped uniqueness, cross-survey reuse of one client identifier, course/survey consistency validation, message sequence uniqueness, and legacy null compatibility.
- [x] Run `manage.py test datapipeline.tests.test_response_sessions.ResponseSessionModelTests --verbosity 2`; verify failures identify the missing model/fields.
- [x] Add the minimal model definitions, constraints, indexes, and read-only admin surface.
- [x] Generate migration `0047` with `manage.py makemigrations datapipeline` and inspect it to confirm there is no data operation.
- [x] Re-run the focused model tests and confirm they pass.

### Task 2: Add atomic response-message persistence

**Files:**
- Create: `datapipeline/response_sessions.py`
- Modify: `datapipeline/tests/test_response_sessions.py`

**Interfaces:**
- Produces: `persist_feedback_messages(payloads: list[dict]) -> list[FeedbackMessage]` and `ResponseWriteValidationError` carrying a stable code and optional item index.
- Consumes: `ResponseSession`, `FeedbackGPT`, and `FeedbackMessage` from Task 1.

- [x] Add service tests proving session reuse, one-based contiguous sequence allocation, distinct sessions across surveys, complete prevalidation, and rollback on a failing multi-item batch.
- [x] Run the focused service tests and verify they fail because the service does not exist.
- [x] Implement payload normalization, survey resolution, deterministic session locking, sequence-range allocation, and one atomic `bulk_create`.
- [x] Re-run the focused service tests and confirm they pass.

### Task 3: Route student write endpoints through the service

**Files:**
- Modify: `datapipeline/views.py`
- Modify: `datapipeline/tests/test_response_sessions.py`
- Modify: `datapipeline/tests/test_feedback_field_attribution.py` only if an existing assertion needs to verify the new authoritative path.

**Interfaces:**
- Consumes: `persist_feedback_messages` and `ResponseWriteValidationError` from Task 2.
- Produces: existing endpoint responses plus response-session metadata; HTTP 400 for invalid occurrence identity; HTTP 500 for unexpected persistence failure.

- [x] Add endpoint tests for single-write linkage and metadata, same-client cross-survey isolation, bulk grouping/sequences, invalid survey rejection, and all-or-nothing failure.
- [x] Run those tests and verify they fail because the endpoints still write loose messages.
- [x] Replace direct single/bulk construction with the service while preserving URLs, request payloads, success status, and `saved` count.
- [x] Run the response-session and field-attribution tests.
- [x] Run `manage.py makemigrations --check --dry-run`, `manage.py migrate --plan`, `manage.py check`, the complete `datapipeline.tests` suite, and `git diff --check`.
- [x] Inspect the final diff for accidental backfill, read-cutover, frontend, research, or production changes.
- [x] Commit the bounded backend slice locally; do not push.
