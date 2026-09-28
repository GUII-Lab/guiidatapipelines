"""Canonical, course-authorized Prompt Designer endpoints."""

import hashlib
import json
import secrets
from datetime import timedelta

from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_protect

from leai.models.authoring import (
    AuthoringConversation, AuthoringMessage, AuthoringRun, PreviewDecision,
    PreviewMessage, PreviewSession, QuestionSet, QuestionSetDraft,
    QuestionSetTemplate, QuestionSetRevision, SurveyOccurrence,
)
from leai.models.identity import Course
from leai.models.jobs import DomainJob
from leai.models.responses import TeamConfiguration, TeamDefinition, TeamSnapshot, TeamSnapshotItem
from leai.services.actions import has_course_action
from leai.services.authoring_wizard import (
    AuthoringConflict, create_version, freeze_draft, initial_body,
    publish_revision, save_body, template_catalog, validate_body,
)
from leai.services.instructor_sessions import resolve_instructor_session
from leai.services.jobs import enqueue_domain_job, public_job_status, start_domain_job_thread
from leai.services.mutation_receipts import IdempotencyConflict, execute_once
from .environment import no_store_json


def _method(methods):
    response = HttpResponseNotAllowed(methods)
    response["Cache-Control"] = "no-store"
    return response


def _error(code, status):
    return no_store_json({"error": code}, status=status)


def _context(request, course_id, action="feedback.author"):
    session = resolve_instructor_session(request)
    if session is None:
        return None, None, _error("authentication_required", 401)
    course = Course.objects.filter(public_id=course_id, lifecycle_state="active").first()
    if course is None or not has_course_action(session.account, course, action):
        return None, None, _error("not_found", 404)
    return session.account, course, None


def _payload(request, required=(), optional=(), *, max_bytes=65536):
    if request.content_type != "application/json" or len(request.body) > max_bytes:
        raise ValueError("invalid_request")
    try:
        value = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("invalid_request") from error
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("invalid_request")
    return value


def _key(request):
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 1024:
        raise ValueError("idempotency_key_required")
    return key


def _once(request, actor, operation, target, payload, callback):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return execute_once(
        principal_scope=f"instructor-account:{actor.pk}", operation=operation,
        target_key=target, idempotency_key=_key(request),
        request_hash=hashlib.sha256(canonical.encode()).hexdigest(), mutate=callback,
    )


def _set(course, question_set_id):
    return QuestionSet.objects.filter(course=course, public_id=question_set_id, draft__isnull=False).select_related("draft").first()


def _draft_dto(question_set):
    draft = question_set.draft
    latest = question_set.revisions.order_by("-revision_number").first()
    return {
        "id": str(question_set.public_id), "title": question_set.title,
        "audience": question_set.audience, "collection_style": question_set.collection_style,
        "draft_version": draft.current_version, "body": draft.canonical_body,
        "updated_at": draft.updated_at.isoformat(),
        "resumable": not latest or latest.source_draft_version.version_number != draft.current_version,
    }


def _revision_dto(revision):
    decision = PreviewDecision.objects.filter(revision=revision).first()
    return {
        "id": str(revision.public_id), "question_set_id": str(revision.question_set.public_id),
        "revision_number": revision.revision_number,
        "source_draft_version": revision.source_draft_version.version_number,
        "content_hash": revision.content_hash,
        "body": revision.compiled_protocol,
        "preview_decision": decision.decision if decision else None,
        "created_at": revision.created_at.isoformat(),
    }


def _survey_dto(row):
    return {
        "id": str(row.public_id), "question_set_id": str(row.revision.question_set.public_id),
        "revision_id": str(row.revision.public_id), "label": row.label,
        "audience": row.revision.question_set.audience,
        "collection_style": row.revision.question_set.collection_style,
        "state": "closed" if row.manually_closed_at else "scheduled" if row.opens_at and row.opens_at > timezone.now() else "open",
        "direct_url": f"feedback.html?id={row.public_id}",
        "opens_at": row.opens_at.isoformat() if row.opens_at else None,
        "closes_at": row.closes_at.isoformat() if row.closes_at else None,
        "team_setup_required": row.revision.question_set.audience == "team" and not row.team_snapshot_items.exists(),
        "completion_certificate_enabled": row.completion_certificate_enabled,
        "completed_response_download_enabled": row.completed_response_download_enabled,
        "allowed_actions": ["copy_link"] if row.management_mode == "managed" else [],
    }


@csrf_protect
def templates_view(request, course_id):
    if request.method != "GET":
        return _method(["GET"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    templates = [{**item, "source": "leai"} for item in template_catalog()]
    rows = QuestionSetTemplate.objects.filter(owner_account=actor) | QuestionSetTemplate.objects.filter(owner_institution=course.institution)
    for row in rows.distinct().order_by("title", "pk")[:100]:
        revision = row.revisions.order_by("-revision_number").first()
        if revision:
            templates.append({
                "id": str(revision.public_id), "name": row.title, "description": "",
                "audience": "individual", "collection_style": "guided",
                "source": "my" if row.owner_account_id == actor.pk else "community",
            })
    return no_store_json({"templates": templates})


@csrf_protect
def question_sets_view(request, course_id):
    if request.method not in ("GET", "POST"):
        return _method(["GET", "POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    if request.method == "GET":
        rows = QuestionSet.objects.filter(course=course, draft__isnull=False).select_related("draft").order_by("-updated_at", "-pk")[:200]
        return no_store_json({"question_sets": [_draft_dto(row) for row in rows]})
    try:
        data = _payload(request, ("title", "audience", "collection_style"), ("template_id",))
        title = data["title"]
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("invalid_title")
        template_id = data.get("template_id")
        if template_id is not None and not isinstance(template_id, str):
            raise ValueError("invalid_template")
        template_revision = None
        if template_id and template_id not in {item["id"] for item in template_catalog()}:
            template_revision = QuestionSetTemplate.objects.filter(
                revisions__public_id=template_id,
            ).filter(owner_account=actor).first()
            if template_revision is None:
                template_revision = QuestionSetTemplate.objects.filter(
                    revisions__public_id=template_id, owner_institution=course.institution,
                ).first()
            if template_revision is None:
                return _error("not_found", 404)
            revision = template_revision.revisions.get(public_id=template_id)
            body = validate_body(revision.canonical_body)
        else:
            body = initial_body(
                title=title.strip(), audience=data["audience"],
                collection_style=data["collection_style"], template_id=template_id,
            )
        body = {**body, "title": title.strip()}
        validate_body(body)
        def mutate():
            question_set = QuestionSet.objects.create(
                course=course, owner=actor, title=body["title"],
                audience=data["audience"], collection_style=data["collection_style"],
            )
            draft = QuestionSetDraft.objects.create(
                question_set=question_set, current_version=1, canonical_body=body, updated_by=actor,
            )
            create_version(draft, actor, body)
            AuthoringConversation.objects.create(
                question_set=question_set, origin_surface="builder", created_by=actor,
            )
            return {"question_set_id": str(question_set.public_id)}
        result, _ = _once(request, actor, "question_set_create", str(course.public_id), data, mutate)
        return no_store_json(_draft_dto(_set(course, result["question_set_id"])), status=201)
    except IdempotencyConflict:
        return _error("idempotency_conflict", 409)
    except ValueError as error:
        return _error(str(error) if str(error) in ("idempotency_key_required", "unknown_template", "unsupported_feedback_type") else "invalid_request", 400)


@csrf_protect
def question_set_view(request, course_id, question_set_id):
    if request.method != "GET":
        return _method(["GET"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    question_set = _set(course, question_set_id)
    return no_store_json(_draft_dto(question_set)) if question_set else _error("not_found", 404)


@csrf_protect
def draft_view(request, course_id, question_set_id):
    if request.method not in ("GET", "PATCH"):
        return _method(["GET", "PATCH"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    question_set = _set(course, question_set_id)
    if not question_set:
        return _error("not_found", 404)
    if request.method == "GET":
        return no_store_json(_draft_dto(question_set))
    try:
        data = _payload(request, ("expected_version", "body"))
        if type(data["expected_version"]) is not int or data["expected_version"] < 1:
            raise ValueError("invalid_request")
        validate_body(data["body"])
        def mutate():
            updated, changed = save_body(
                question_set.draft, actor, expected_version=data["expected_version"], body=data["body"],
            )
            return {"question_set_id": str(question_set.public_id), "changed": changed}
        result, _ = _once(request, actor, "question_set_save", str(question_set.public_id), data, mutate)
        return no_store_json({**_draft_dto(_set(course, question_set_id)), "changed": result["changed"]})
    except AuthoringConflict:
        return _error("stale_draft", 409)
    except IdempotencyConflict:
        return _error("idempotency_conflict", 409)
    except ValueError:
        return _error("invalid_request", 400)


@csrf_protect
def versions_view(request, course_id, question_set_id):
    if request.method != "GET":
        return _method(["GET"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    question_set = _set(course, question_set_id)
    if not question_set:
        return _error("not_found", 404)
    rows = question_set.draft.versions.order_by("-version_number")[:100]
    return no_store_json({"versions": [
        {"id": str(row.pk), "number": row.version_number, "body": row.canonical_body,
         "change_kind": row.change_kind, "created_at": row.created_at.isoformat()}
        for row in rows
    ]})


@csrf_protect
def restore_view(request, course_id, question_set_id):
    if request.method != "POST":
        return _method(["POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    question_set = _set(course, question_set_id)
    if not question_set:
        return _error("not_found", 404)
    try:
        data = _payload(request, ("expected_version", "version_id"))
        if type(data["expected_version"]) is not int or not isinstance(data["version_id"], str) or not data["version_id"].isdigit():
            raise ValueError("invalid_request")
        source = question_set.draft.versions.filter(pk=int(data["version_id"])).first()
        if source is None:
            return _error("not_found", 404)
        def mutate():
            saved, changed = save_body(
                question_set.draft, actor, expected_version=data["expected_version"],
                body=source.canonical_body, change_kind="restore",
            )
            return {"draft_version": saved.current_version, "changed": changed}
        _once(request, actor, "question_set_restore", str(question_set.public_id), data, mutate)
        return no_store_json(_draft_dto(_set(course, question_set_id)))
    except AuthoringConflict:
        return _error("stale_draft", 409)
    except IdempotencyConflict:
        return _error("idempotency_conflict", 409)
    except ValueError:
        return _error("invalid_request", 400)


@csrf_protect
def freeze_view(request, course_id, question_set_id):
    if request.method != "POST":
        return _method(["POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    question_set = _set(course, question_set_id)
    if not question_set:
        return _error("not_found", 404)
    try:
        data = _payload(request, ("expected_version",))
        if type(data["expected_version"]) is not int or data["expected_version"] < 1:
            raise ValueError("invalid_request")
        revision = freeze_draft(question_set.draft, actor, expected_version=data["expected_version"])
        return no_store_json({"revision": _revision_dto(revision)})
    except AuthoringConflict:
        return _error("stale_draft", 409)
    except ValueError:
        return _error("invalid_request", 400)


@csrf_protect
def revisions_view(request, course_id, question_set_id):
    if request.method != "GET":
        return _method(["GET"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    question_set = _set(course, question_set_id)
    if not question_set:
        return _error("not_found", 404)
    return no_store_json({"revisions": [_revision_dto(row) for row in question_set.revisions.order_by("-revision_number")[:50]]})


def _revision(course, revision_id):
    return QuestionSetRevision.objects.select_related("question_set", "source_draft_version").filter(
        public_id=revision_id, question_set__course=course,
    ).first()


@csrf_protect
def preview_view(request, course_id, revision_id):
    if request.method not in ("GET", "POST"):
        return _method(["GET", "POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    revision = _revision(course, revision_id)
    if revision is None:
        return _error("not_found", 404)
    if request.method == "GET":
        return no_store_json(_revision_dto(revision))
    preview = PreviewSession.objects.filter(revision=revision, actor=actor, expires_at__gt=timezone.now()).order_by("-created_at").first()
    if preview is None:
        token = secrets.token_urlsafe(32)
        preview = PreviewSession.objects.create(
            revision=revision, actor=actor,
            capability_digest=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(hours=12),
        )
    if not preview.messages.exists():
        items = [item for section in revision.compiled_protocol["sections"] for item in section["items"]]
        with transaction.atomic():
            locked = PreviewSession.objects.select_for_update().get(pk=preview.pk)
            if not locked.messages.exists():
                PreviewMessage.objects.create(preview_session=locked, sequence=1, role="assistant",
                                              content=revision.compiled_protocol["intro"], attribution={"phase": "intro"})
                PreviewMessage.objects.create(preview_session=locked, sequence=2, role="assistant",
                                              content=items[0]["prompt"], attribution={"item_id": items[0]["id"]})
    return no_store_json({
        "preview_id": str(preview.public_id), "revision": _revision_dto(revision),
        "messages": [
            {"id": str(row.pk), "role": row.role, "content": row.content,
             "item_id": row.attribution.get("item_id"), "created_at": row.created_at.isoformat()}
            for row in preview.messages.order_by("sequence")
        ],
    })


@csrf_protect
def preview_messages_view(request, course_id, preview_id):
    if request.method != "POST":
        return _method(["POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    preview = PreviewSession.objects.select_related("revision__question_set").filter(
        public_id=preview_id, actor=actor, revision__question_set__course=course,
        expires_at__gt=timezone.now(),
    ).first()
    if preview is None:
        return _error("not_found", 404)
    try:
        data = _payload(request, ("item_id", "content"), max_bytes=5000)
        items = [item for section in preview.revision.compiled_protocol["sections"] for item in section["items"]]
        answered = list(preview.messages.filter(role="student").order_by("sequence"))
        if len(answered) >= len(items) or data["item_id"] != items[len(answered)]["id"] or not isinstance(data["content"], str) or not 1 <= len(data["content"].strip()) <= 3000:
            raise ValueError("invalid_preview_turn")
        with transaction.atomic():
            locked = PreviewSession.objects.select_for_update().get(pk=preview.pk)
            count = locked.messages.filter(role="student").count()
            if count != len(answered):
                raise AuthoringConflict("preview_advanced")
            row = PreviewMessage.objects.create(
                preview_session=locked, sequence=(locked.messages.order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1,
                role="student", content=data["content"].strip(), attribution={"item_id": data["item_id"]},
            )
            if count + 1 < len(items):
                next_item = items[count + 1]
                PreviewMessage.objects.create(preview_session=locked, sequence=row.sequence + 1,
                                              role="assistant", content=next_item["prompt"],
                                              attribution={"item_id": next_item["id"]})
            else:
                PreviewMessage.objects.create(preview_session=locked, sequence=row.sequence + 1,
                                              role="assistant", content="Thank you for sharing your feedback.",
                                              attribution={"phase": "acknowledgement"})
        return no_store_json({"id": str(row.pk), "answered_count": count + 1}, status=201)
    except AuthoringConflict:
        return _error("preview_advanced", 409)
    except ValueError:
        return _error("invalid_request", 400)


@csrf_protect
def preview_decision_view(request, course_id, revision_id):
    if request.method != "POST":
        return _method(["POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    revision = _revision(course, revision_id)
    if revision is None:
        return _error("not_found", 404)
    try:
        data = _payload(request, ("decision",))
        if data["decision"] not in ("completed", "skipped"):
            raise ValueError("invalid_decision")
        existing = PreviewDecision.objects.filter(revision=revision).first()
        if existing:
            return no_store_json(_revision_dto(revision)) if existing.decision == data["decision"] else _error("preview_decision_final", 409)
        if data["decision"] == "completed":
            items = [item["id"] for section in revision.compiled_protocol["sections"] for item in section["items"]]
            preview = PreviewSession.objects.filter(revision=revision, actor=actor, expires_at__gt=timezone.now()).order_by("-created_at").first()
            trace = list(preview.messages.order_by("sequence").values("role", "content", "attribution")) if preview else []
            expected = [{"role": "assistant", "content": revision.compiled_protocol["intro"], "attribution": {"phase": "intro"}}]
            authored = [item for section in revision.compiled_protocol["sections"] for item in section["items"]]
            for item in authored:
                expected.append({"role": "assistant", "content": item["prompt"], "attribution": {"item_id": item["id"]}})
                expected.append({"role": "student", "attribution": {"item_id": item["id"]}})
            expected.append({"role": "assistant", "content": "Thank you for sharing your feedback.",
                             "attribution": {"phase": "acknowledgement"}})
            if len(trace) != len(expected) or any(any(actual.get(key) != value for key, value in wanted.items())
                                                     for actual, wanted in zip(trace, expected)):
                return _error("preview_incomplete", 409)
        PreviewDecision.objects.create(
            revision=revision, actor=actor, decision=data["decision"],
            idempotency_key_hash=hashlib.sha256(
                f"{revision.pk}:{actor.pk}:{_key(request)}".encode()
            ).hexdigest(),
        )
        return no_store_json(_revision_dto(revision))
    except ValueError:
        return _error("invalid_request", 400)


def _date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_date")
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise ValueError("invalid_date")
    return parsed


@csrf_protect
def publish_view(request, course_id, revision_id):
    if request.method != "POST":
        return _method(["POST"])
    actor, course, error = _context(request, course_id, "feedback.publish")
    if error:
        return error
    revision = _revision(course, revision_id)
    if revision is None:
        return _error("not_found", 404)
    try:
        data = _payload(request, ("label",), ("opens_at", "closes_at", "completion_certificate_enabled", "completed_response_download_enabled"))
        label = data["label"]
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 200:
            raise ValueError("invalid_label")
        opens_at, closes_at = _date(data.get("opens_at")), _date(data.get("closes_at"))
        certificate = data.get("completion_certificate_enabled", False)
        download = data.get("completed_response_download_enabled", False)
        if type(certificate) is not bool or type(download) is not bool:
            raise ValueError("invalid_output_settings")
        def mutate():
            occurrence, _ = publish_revision(
                revision, actor, label=label.strip(), opens_at=opens_at, closes_at=closes_at,
                completion_certificate_enabled=certificate, completed_response_download_enabled=download,
            )
            return {"occurrence_id": str(occurrence.public_id)}
        result, _ = _once(request, actor, "question_set_publish", str(revision.public_id), data, mutate)
        occurrence = SurveyOccurrence.objects.select_related("revision__question_set").get(
            public_id=result["occurrence_id"], course=course,
        )
        return no_store_json(_survey_dto(occurrence), status=201)
    except AuthoringConflict as error:
        return _error(str(error), 409)
    except IdempotencyConflict:
        return _error("idempotency_conflict", 409)
    except ValueError:
        return _error("invalid_request", 400)


@csrf_protect
def surveys_view(request, course_id):
    if request.method != "GET":
        return _method(["GET"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    rows = SurveyOccurrence.objects.filter(course=course).select_related("revision__question_set").order_by("-created_at", "-pk")[:200]
    return no_store_json({"surveys": [_survey_dto(row) for row in rows]})


@csrf_protect
def survey_teams_view(request, course_id, survey_id):
    if request.method != "POST":
        return _method(["POST"])
    actor, course, error = _context(request, course_id, "feedback.publish")
    if error:
        return error
    occurrence = SurveyOccurrence.objects.select_related("revision__question_set").filter(
        course=course, public_id=survey_id, management_mode="managed",
        revision__question_set__audience="team",
    ).first()
    if occurrence is None:
        return _error("not_found", 404)
    try:
        data = _payload(request, ("labels",), max_bytes=8192)
        labels = data["labels"]
        if (not isinstance(labels, list) or not 1 <= len(labels) <= 30
                or any(not isinstance(label, str) or not 1 <= len(label.strip()) <= 200 for label in labels)
                or len({label.strip().casefold() for label in labels}) != len(labels)):
            raise ValueError("invalid_team_labels")
        normalized = [label.strip() for label in labels]
        def mutate():
            with transaction.atomic():
                locked = SurveyOccurrence.objects.select_for_update().get(pk=occurrence.pk)
                if locked.team_snapshot_items.exists() or locked.response_sessions.exists():
                    raise AuthoringConflict("team_setup_final")
                configuration = TeamConfiguration.objects.create(
                    course=course, name=f"{locked.label} teams", created_by=actor,
                )
                snapshot = TeamSnapshot.objects.create(
                    occurrence=locked, source_configuration=configuration,
                    course=course, frozen_at=None,
                )
                for number, label in enumerate(normalized, 1):
                    stable_key = f"team-{number}"
                    TeamDefinition.objects.create(configuration=configuration, stable_key=stable_key,
                                                  label=label, sort_order=number)
                    TeamSnapshotItem.objects.create(snapshot=snapshot, occurrence=locked,
                                                    item_number=number, stable_key=stable_key, label=label)
                snapshot.frozen_at = timezone.now()
                snapshot.save(update_fields=("frozen_at",))
                return {"survey_id": str(locked.public_id)}
        _once(request, actor, "survey_team_setup", str(survey_id), data, mutate)
        return no_store_json(_survey_dto(SurveyOccurrence.objects.select_related("revision__question_set").get(pk=occurrence.pk)))
    except AuthoringConflict as error:
        return _error(str(error), 409)
    except IdempotencyConflict:
        return _error("idempotency_conflict", 409)
    except ValueError:
        return _error("invalid_request", 400)


@csrf_protect
def authoring_conversation_view(request, course_id, question_set_id):
    if request.method != "GET":
        return _method(["GET"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    question_set = _set(course, question_set_id)
    if question_set is None:
        return _error("not_found", 404)
    conversation = AuthoringConversation.objects.filter(question_set=question_set).first()
    rows = conversation.messages.order_by("sequence") if conversation else []
    return no_store_json({"messages": [
        {"id": str(row.pk), "role": row.role, "content": row.content,
         "created_at": row.created_at.isoformat()}
        for row in rows if row.role in ("user", "assistant")
    ]})


@csrf_protect
def ai_runs_view(request, course_id, question_set_id):
    if request.method != "POST":
        return _method(["POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    question_set = _set(course, question_set_id)
    if question_set is None:
        return _error("not_found", 404)
    try:
        data = _payload(request, ("content", "expected_version"), max_bytes=4096)
        if not isinstance(data["content"], str) or not 1 <= len(data["content"].strip()) <= 3000 or type(data["expected_version"]) is not int or data["expected_version"] < 1:
            raise ValueError("invalid_request")
        def mutate():
            with transaction.atomic():
                draft = QuestionSetDraft.objects.select_for_update().get(question_set=question_set)
                if draft.current_version != data["expected_version"]:
                    raise AuthoringConflict("stale_draft")
                conversation, _ = AuthoringConversation.objects.get_or_create(
                    question_set=question_set,
                    defaults={"origin_surface": "builder", "created_by": actor},
                )
                if conversation.runs.filter(status__in=("pending", "running")).exists():
                    raise AuthoringConflict("authoring_in_progress")
                sequence = (conversation.messages.order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1
                AuthoringMessage.objects.create(
                    conversation=conversation, sequence=sequence, role="user", input_method="typed",
                    content=data["content"].strip(),
                )
                base = draft.versions.get(version_number=draft.current_version)
                run = AuthoringRun.objects.create(
                    conversation=conversation, base_draft_version=base,
                    requested_by=actor, status="pending",
                    source_provenance_snapshot={"origin_surface": "builder"},
                )
                job = enqueue_domain_job(
                    job_type="authoring_ai_run", course=course, actor=actor,
                    payload={"authoring_run_id": str(run.pk)},
                )
                return {"job_id": str(job.public_id)}
        result, _ = _once(request, actor, "authoring_ai_run", str(question_set.public_id), data, mutate)
        transaction.on_commit(lambda: start_domain_job_thread(result["job_id"]))
        return no_store_json(result, status=202)
    except AuthoringConflict as error:
        return _error(str(error), 409)
    except IdempotencyConflict:
        return _error("idempotency_conflict", 409)
    except ValueError:
        return _error("invalid_request", 400)
