"""Versioned instructor authoring for the React feedback Builder.

The draft body is the version-1 student protocol. This avoids a second authoring
schema: the same validator guards manual edits, AI proposals and publication.
"""

import hashlib
import json
from copy import deepcopy

from django.db import transaction
from leai.models.authoring import (
    QuestionSetDraftVersion, QuestionSetRevision, SurveyOccurrence,
)
from leai.services.protocol import validate_protocol


class AuthoringConflict(Exception):
    pass


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_body(body):
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 65536:
        raise ValueError("draft_too_large")
    validate_protocol(body)
    if len(body["sections"]) > 12 or sum(len(section["items"]) for section in body["sections"]) > 24:
        raise ValueError("too_many_questions")
    for section in body["sections"]:
        if len(section["title"]) > 200:
            raise ValueError("section_title_too_long")
        for item in section["items"]:
            if len(item["prompt"]) > 4000 or len(item["reflection_goal"]) > 1000:
                raise ValueError("question_too_long")
    return body


def validate_for_style(body, question_set):
    validate_body(body)
    if question_set.collection_style == "open":
        if len(body["sections"]) != 1 or len(body["sections"][0]["items"]) != 2:
            raise ValueError("open_conversation_requires_opening_and_closing")
    return body


def _item(identifier, prompt, goal):
    return {
        "id": identifier, "prompt": prompt, "wording": "adaptive",
        "response": {"kind": "text"}, "reflection_goal": goal,
        "coverage_targets": [], "example_probes": [], "max_additional_probes": 1,
    }


# Exact prompts are adapted from the approved legacy V12 system templates.
_TEMPLATES = (
    ("weekly-reflection", "Weekly reflection", "A balanced weekly check-in on learning, difficulty, and next steps.",
     "Weekly learning reflection", "A short guided conversation about this week’s learning experience.",
     [("What stood out", "What idea, activity, or moment stood out most to you this week, and why?"),
      ("What was difficult", "What felt confusing, difficult, or slower than you expected this week?"),
      ("Learning support", "What did the instructor, course materials, or your classmates do that helped your learning?"),
      ("Next step", "What is one concrete step you will take before the next class?")]),
    ("mid-course-check-in", "Mid-course check-in", "A broader check on course pace, support, confidence, and priorities.",
     "Mid-course learning check-in", "A guided conversation to understand how the course is working so far.",
     [("Current confidence", "How confident do you feel about the main ideas and skills covered so far?"),
      ("Course pace", "How is the pace of the course working for you right now?"),
      ("Helpful support", "Which course activity or resource has helped your learning most so far?"),
      ("Priority", "What should the instructor prioritize during the rest of the course?")]),
    ("project-milestone", "Project milestone reflection", "An individual reflection on progress, decisions, obstacles, and the next milestone.",
     "Project milestone reflection", "A guided individual reflection on your project work and next decisions.",
     [("Progress", "What meaningful progress did you make toward this milestone?"),
      ("Important decision", "What was the most important decision you made, and what informed it?"),
      ("Obstacle", "What obstacle or uncertainty affected your work most?"),
      ("Next milestone", "What is your most important next action before the next milestone?")]),
)


def template_catalog():
    return [{"id": row[0], "name": row[1], "description": row[2], "audience": "individual",
             "collection_style": "guided"} for row in _TEMPLATES]


def initial_body(*, title, audience, collection_style, template_id=None):
    if audience not in ("individual", "team") or collection_style not in ("guided", "open") or (audience, collection_style) == ("team", "open"):
        raise ValueError("unsupported_feedback_type")
    template = next((row for row in _TEMPLATES if row[0] == template_id), None)
    if template_id is not None and (template is None or (audience, collection_style) != ("individual", "guided")):
        raise ValueError("unknown_template")
    if template:
        title = template[3]
        intro = template[4]
        sections = [
            {"id": f"s{index}", "title": label, "items": [_item(f"q{index}", prompt, label)]}
            for index, (label, prompt) in enumerate(template[5], 1)
        ]
    elif audience == "team":
        intro = "Reflect privately on collaboration inside the team you selected. Do not name or rate teammates."
        sections = [
            {"id": "s1", "title": "How the team worked", "items": [
                _item("q1", "How was work divided inside your team for this milestone?", "Understand work distribution inside this team."),
                _item("q2", "How well did your team communicate while working through decisions or problems?", "Understand communication inside this team."),
            ]},
            {"id": "s2", "title": "What should change next", "items": [
                _item("q3", "What is one specific contribution you made to your team's work?", "Understand the student's own contribution."),
                _item("q4", "What is one thing your team should change before the next milestone?", "Understand one within-team improvement."),
            ]},
        ]
    elif collection_style == "open":
        intro = "Share your experience with this course in your own words."
        sections = [{"id": "s1", "title": "Open conversation", "items": [
            _item("q1", "How is your learning experience going right now?", "Listen for learning supports, blockers, expectations, workload, and suggestions."),
            _item("q2", "Is there anything else you want your instructor to know?", "Make room for anything the student has not yet shared."),
        ]}]
        sections[0]["items"][0]["max_additional_probes"] = 3
        sections[0]["items"][1]["max_additional_probes"] = 0
    else:
        intro = "Share your experience with this course."
        sections = [{"id": "s1", "title": "Your experience", "items": [_item("q1", "What stood out in your learning experience?", "Understand the learner's experience.")]}]
    return validate_body({"version": 1, "title": title, "intro": intro, "scales": {}, "sections": sections})


def create_version(draft, actor, body, *, change_kind="manual"):
    return QuestionSetDraftVersion.objects.create(
        draft=draft, version_number=draft.current_version, content_hash=digest(body),
        canonical_body=deepcopy(body), change_kind=change_kind, created_by=actor,
    )


def save_body(draft, actor, *, expected_version, body, change_kind="manual"):
    validate_for_style(body, draft.question_set)
    with transaction.atomic():
        locked = type(draft).objects.select_for_update().get(pk=draft.pk)
        if locked.current_version != expected_version:
            raise AuthoringConflict("stale_draft")
        if digest(locked.canonical_body) == digest(body):
            return locked, False
        locked.current_version += 1
        locked.canonical_body = deepcopy(body)
        locked.updated_by = actor
        locked.save(update_fields=("current_version", "canonical_body", "updated_by", "updated_at"))
        create_version(locked, actor, body, change_kind=change_kind)
        locked.question_set.title = body["title"]
        locked.question_set.save(update_fields=("title", "updated_at"))
        return locked, True


def freeze_draft(draft, actor, *, expected_version):
    with transaction.atomic():
        locked = type(draft).objects.select_for_update().get(pk=draft.pk)
        if locked.current_version != expected_version:
            raise AuthoringConflict("stale_draft")
        body = validate_for_style(locked.canonical_body, locked.question_set)
        source = locked.versions.get(version_number=locked.current_version)
        existing = QuestionSetRevision.objects.filter(question_set=locked.question_set, source_draft_version=source).first()
        if existing:
            return existing
        latest = QuestionSetRevision.objects.filter(question_set=locked.question_set).order_by("-revision_number").first()
        return QuestionSetRevision.objects.create(
            question_set=locked.question_set,
            revision_number=(latest.revision_number if latest else 0) + 1,
            source_draft_version=source, content_hash=digest(body),
            compiled_protocol=deepcopy(body), compiler_version="protocol-v1",
            engine_version="response-flow-v1", created_by=actor,
        )


def publish_revision(revision, actor, *, label, opens_at=None, closes_at=None,
                     completion_certificate_enabled=False, completed_response_download_enabled=False):
    if not hasattr(revision, "preview_decision"):
        raise AuthoringConflict("preview_required")
    if opens_at and closes_at and opens_at >= closes_at:
        raise ValueError("invalid_schedule")
    with transaction.atomic():
        existing = SurveyOccurrence.objects.select_for_update().filter(revision=revision, provenance="native").first()
        if existing:
            if (existing.label != label or existing.opens_at != opens_at or existing.closes_at != closes_at
                    or existing.completion_certificate_enabled != completion_certificate_enabled
                    or existing.completed_response_download_enabled != completed_response_download_enabled):
                raise AuthoringConflict("already_published")
            return existing, False
        occurrence = SurveyOccurrence.objects.create(
            revision=revision, course=revision.question_set.course, created_by=actor,
            label=label, opens_at=opens_at, closes_at=closes_at,
            completion_certificate_enabled=completion_certificate_enabled,
            completed_response_download_enabled=completed_response_download_enabled,
        )
        return occurrence, True
