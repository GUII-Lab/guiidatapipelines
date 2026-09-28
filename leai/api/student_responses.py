"""Anonymous student sessions for immutable structured-question revisions."""

import hashlib
import hmac
import json
import re
import secrets
from copy import deepcopy

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from datapipeline.openai_client import OpenAIClientError
from leai.models import ResponseMessage, ResponseSession, SurveyOccurrence
from leai.services.instructor_sessions import TOKEN_PATTERN
from leai.services.protocol import ProtocolError, validate_protocol
from leai.services.response_flow import FlowError, advance_flow, apply_turn, begin_flow, current_prompt
from leai.services.revision_assessment import classify_revision
from leai.services.structured_assessment import AssessmentError, assess_text
from leai.services.actions import has_researcher_course_access
from leai.services.instructor_sessions import resolve_instructor_session

from .environment import no_store_json


def _explicit_decline(text):
    normalized = text.strip().replace("’", "'").strip(".!?。？！ ").casefold()
    return normalized in {"i don't know", "i do not know", "i'm not sure", "i am not sure",
                          "no idea", "我不知道", "不知道", "不清楚"}


def _revision_cue(text):
    return bool(re.search(r"\b(revise|revision|change|correct|earlier|previous|go back|add to my answer|"
                          r"i meant|actually)\b|修改|更正|补充|之前|前面|我刚才说",
                          text, re.IGNORECASE))


def _occurrence(public_id):
    return SurveyOccurrence.objects.select_related("revision__question_set", "course").filter(public_id=public_id).first()


def _available(occurrence):
    now = timezone.now()
    return (occurrence.revision.question_set.audience == "individual"
            and occurrence.course.lifecycle_state == "active"
            and occurrence.manually_closed_at is None
            and (occurrence.opens_at is None or occurrence.opens_at <= now)
            and (occurrence.closes_at is None or occurrence.closes_at > now))


def _json_body(request, *, max_bytes=4096):
    if request.content_type != "application/json" or len(request.body) > max_bytes:
        raise ValueError("invalid JSON request")
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("invalid JSON request") from error
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def _new_capability(occurrence_id):
    nonce = secrets.token_urlsafe(24)
    raw = hmac.new(settings.SECRET_KEY.encode("utf-8"),
                   f"leai-student-v1:{occurrence_id}:{nonce}".encode("ascii"),
                   hashlib.sha256).hexdigest()
    return nonce, raw, hashlib.sha256(raw.encode("ascii")).hexdigest()


def _resolve_session(request, occurrence, session_id):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if not TOKEN_PATTERN.fullmatch(token):
        return None
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    return ResponseSession.objects.filter(
        public_id=session_id, occurrence=occurrence, source="student",
        capability_digest=digest,
    ).first()


def _messages(session):
    return [{
        "id": message.pk,
        "sequence": message.sequence,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "attribution": {key: value for key, value in message.attribution.items()
                        if key != "orchestration_metrics"},
    } for message in session.messages.order_by("sequence")]


def _progress_label(protocol, state):
    index = state["item_index"]
    for area_number, section in enumerate(protocol["sections"], start=1):
        count = len(section["items"])
        if index < count:
            return (f"Area {area_number} of {len(protocol['sections'])} — {section['title']}"
                    f" · Question {index + 1} of {count}")
        index -= count
    return "Reflection complete"


def _session_payload(session):
    protocol = session.occurrence.revision.compiled_protocol
    state = session.flow_state
    student_messages = {m.sequence: m.content for m in session.messages.filter(role="student")}
    excerpts = {qid: [student_messages[r["sequence"]][r["start"]:r["end"]]
                      for r in refs if r["sequence"] in student_messages]
                for qid, refs in state.get("evidence", {}).items()}
    return {
        "session_id": str(session.public_id),
        "survey_id": str(session.occurrence.public_id),
        "turn_version": session.turn_version,
        "status": session.status,
        "prompt": current_prompt(protocol, state),
        "progress_label": _progress_label(protocol, state),
        "results": {qid: {k: v for k, v in result.items() if k in ("rating", "status", "probes")}
                    for qid, result in state["results"].items()},
        "answer_map": state.get("answer_map", {}),
        "messages": _messages(session),
        **({"answer_excerpts": excerpts} if "evidence" in state else {}),
    }


@csrf_exempt
def student_survey_view(request, survey_id):
    if request.method != "GET":
        response = HttpResponseNotAllowed(["GET"])
        response["Cache-Control"] = "no-store"
        return response
    occurrence = _occurrence(survey_id)
    if occurrence is None or occurrence.revision.compiled_protocol == {}:
        return no_store_json({"error": "not_found"}, status=404)
    try:
        protocol = validate_protocol(occurrence.revision.compiled_protocol)
    except ProtocolError:
        return no_store_json({"error": "survey_unavailable"}, status=503)
    return no_store_json({
        "survey_id": str(occurrence.public_id),
        "label": occurrence.label,
        "intro": protocol["intro"],
        "available": _available(occurrence),
        "anonymous_matching_enabled": occurrence.course.anonymous_matching_enabled,
        "completion_certificate_enabled": occurrence.completion_certificate_enabled,
        "completed_response_download_enabled": occurrence.completed_response_download_enabled,
    })


@csrf_exempt
def student_sessions_view(request, survey_id):
    if request.method != "POST":
        response = HttpResponseNotAllowed(["POST"])
        response["Cache-Control"] = "no-store"
        return response
    occurrence = _occurrence(survey_id)
    if occurrence is None or occurrence.revision.compiled_protocol == {}:
        return no_store_json({"error": "not_found"}, status=404)
    if not _available(occurrence):
        return no_store_json({"error": "survey_closed"}, status=403)
    try:
        payload = _json_body(request, max_bytes=128)
        if (set(payload) != {"terms_consent", "research_consent"}
                or payload["terms_consent"] is not True
                or type(payload["research_consent"]) is not bool):
            raise ValueError("explicit terms and research consent choices are required")
        protocol = validate_protocol(occurrence.revision.compiled_protocol)
    except ValueError:
        return no_store_json({"error": "invalid_request"}, status=400)
    state = begin_flow(protocol)
    nonce, token, digest = _new_capability(occurrence.pk)
    with transaction.atomic():
        session = ResponseSession.objects.create(
            occurrence=occurrence,
            source="student",
            capability_nonce=nonce,
            capability_digest=digest,
            capability_key_version=1,
            research_consent=payload["research_consent"],
            flow_state=state,
        )
        first = current_prompt(protocol, state)
        ResponseMessage.objects.bulk_create([
            ResponseMessage(response_session=session, sequence=1, role="assistant",
                            content=protocol["intro"], attribution={"phase": "intro"}),
            ResponseMessage(response_session=session, sequence=2, role="assistant",
                            content=first["text"], attribution={"item_id": first["item_id"],
                                                                  "phase": first["phase"], "wording": first["wording"]}),
        ])
        session.next_message_sequence = 3
        session.save(update_fields=["next_message_sequence"])
    return no_store_json({**_session_payload(session), "token": token}, status=201)


@csrf_exempt
def student_session_view(request, survey_id, session_id):
    if request.method != "GET":
        response = HttpResponseNotAllowed(["GET"])
        response["Cache-Control"] = "no-store"
        return response
    occurrence = _occurrence(survey_id)
    if occurrence is None:
        return no_store_json({"error": "not_found"}, status=404)
    session = _resolve_session(request, occurrence, session_id)
    if session is None:
        return no_store_json({"error": "not_found"}, status=404)
    return no_store_json(_session_payload(session))


@csrf_exempt
def student_session_debug_view(request, survey_id, session_id):
    if request.method != "GET":
        response = HttpResponseNotAllowed(["GET"])
        response["Cache-Control"] = "no-store"
        return response
    if settings.LEAI_ENVIRONMENT not in ("local", "qa"):
        return no_store_json({"error": "not_found"}, status=404)
    occurrence = _occurrence(survey_id)
    if occurrence is None:
        return no_store_json({"error": "not_found"}, status=404)
    instructor_session = resolve_instructor_session(request)
    if instructor_session is None:
        return no_store_json({"error": "authentication_required"}, status=401)
    if (not occurrence.course.student_debug_enabled
            or not has_researcher_course_access(instructor_session.account, occurrence.course)):
        return no_store_json({"error": "not_found"}, status=404)
    session = ResponseSession.objects.filter(
        public_id=session_id,
        occurrence=occurrence,
        source="student",
    ).first()
    if session is None:
        return no_store_json({"error": "not_found"}, status=404)

    state = session.flow_state
    messages = list(session.messages.order_by("sequence"))
    next_assistant = {
        message.sequence - 1: message.attribution
        for message in messages if message.role == "assistant"
    }
    responses = []
    for message in messages:
        if message.role != "student":
            continue
        attribution = message.attribution
        responses.append({
            "sequence": message.sequence,
            "item_id": attribution.get("item_id"),
            "phase": attribution.get("phase"),
            "kind": attribution.get("kind"),
            "content": message.content,
            "evidence_for": attribution.get("evidence_for", []),
            "covered_targets": attribution.get("covered_targets", []),
            "next_item_id": next_assistant.get(message.sequence, {}).get("item_id"),
            "next_phase": next_assistant.get(message.sequence, {}).get("phase"),
        })
    return no_store_json({
        "session_id": str(session.public_id),
        "turn_version": session.turn_version,
        "schema_state": {
            "item_index": state["item_index"],
            "phase": state["phase"],
            "results": state["results"],
            "answer_map": state.get("answer_map", {}),
            "evidence_seen": state.get("evidence_seen", {}),
            "coverage_seen": state.get("coverage_seen", {}),
            **({"orchestration": {**{key: state.get(key) for key in (
                "evidence", "superseded_evidence", "presented_main_ids", "last_dialogue_action", "last_turn_diagnostics")},
                "turn_metrics": [{"assistant_sequence": message.sequence, **message.attribution["orchestration_metrics"]}
                                 for message in messages if "orchestration_metrics" in message.attribution]}}
               if "last_turn_diagnostics" in state else {}),
        },
        "responses": responses,
    })


def _revise_answer(session, occurrence, student, expected_version):
    """Append a conversational correction and remap its evidence before final download."""
    protocol = occurrence.revision.compiled_protocol
    try:
        old_prompt = current_prompt(protocol, session.flow_state)
        mapping = classify_revision(protocol, student["text"], current_item_id=old_prompt.get("item_id"))
        if mapping["operation"] == "answer" and old_prompt["phase"] != "complete":
            return None
        if mapping["operation"] == "answer":
            mapping = {"item_id": "", "operation": "clarify", "answer_text": "",
                       "clarification": "Which earlier question would you like to add to or change?"}
        target_id = mapping["item_id"]
        assessment = None
        target = None
        if target_id:
            items = [item for section in protocol["sections"] for item in section["items"]]
            target_index, target = next((index, item) for index, item in enumerate(items)
                                        if item["id"] == target_id)
            assessment_state = deepcopy(session.flow_state)
            assessment_state["item_index"] = target_index
            assessment_state["phase"] = "reflection" if target["response"]["kind"] == "likert" else "answer"
            assessment_state["pending_prompt"] = None
            if mapping["operation"] == "replace":
                assessment_state.setdefault("coverage_seen", {}).pop(target_id, None)
            assessment = assess_text(protocol, assessment_state, mapping["answer_text"])
    except (AssessmentError, OpenAIClientError, KeyError, StopIteration):
        return no_store_json({"error": "assessment_unavailable", "retryable": True}, status=503)

    with transaction.atomic():
        locked = ResponseSession.objects.select_for_update().get(pk=session.pk)
        if locked.status != "active" or locked.turn_version != expected_version:
            return no_store_json({"error": "stale_turn"}, status=409)
        state = deepcopy(locked.flow_state)
        sequence = locked.next_message_sequence
        old_prompt = current_prompt(protocol, state)
        if target_id:
            answer_map = state.setdefault("answer_map", {})
            if mapping["operation"] == "replace":
                answer_map[target_id] = [sequence]
                state.setdefault("coverage_seen", {})[target_id] = []
            else:
                answer_map.setdefault(target_id, []).append(sequence)
            coverage = state.setdefault("coverage_seen", {}).setdefault(target_id, [])
            for covered in assessment["covered_targets"]:
                if covered not in coverage:
                    coverage.append(covered)
            required = {entry["id"] for entry in target.get("coverage_targets", [])}
            sufficient = required <= set(coverage) if required else assessment["sufficient"]
            result = state["results"].setdefault(target_id, {"rating": None, "status": "active", "probes": 0})
            if mapping["operation"] == "replace":
                result["probes"] = 0
            if mapping.get("rating") is not None:
                result["rating"] = mapping["rating"]
            current_index = state["item_index"]
            if sufficient:
                result["status"] = "answered"
                if target_index == current_index:
                    advance_flow(protocol, state)
            elif target_index <= current_index:
                limit = target.get("max_additional_probes", 2)
                followup = assessment.get("followup", "").strip()
                if followup and result["probes"] < limit:
                    if target_index < current_index:
                        state["resume_item_index"] = state.get("resume_item_index", current_index)
                    state["item_index"] = target_index
                    state["phase"] = "probe"
                    state["pending_prompt"] = followup
                    result["probes"] += 1
                    result["status"] = "active"
                else:
                    result["status"] = "partial"
                    if target_index == current_index:
                        advance_flow(protocol, state)
            state.setdefault("evidence_seen", {})[target_id] = True
            for item_id in assessment["evidence_for"]:
                state["evidence_seen"][item_id] = True
                if item_id != target_id:
                    answer_map.setdefault(item_id, []).append(sequence)
            next_prompt = current_prompt(protocol, state)
            update_acknowledgement = (f"Thanks — I’ve updated your response to “{target['prompt']}” "
                                      "based on what you just shared.")
            if next_prompt["phase"] == "complete":
                assistant_text = update_acknowledgement + " You can keep revising or download your reflection."
            elif next_prompt["item_id"] == target_id and next_prompt["phase"] == "probe":
                assistant_text = update_acknowledgement + f"\n\n{next_prompt['text']}"
            else:
                assistant_text = update_acknowledgement + f" We’re on this question now: “{next_prompt['text']}”"
            attribution = {"item_id": target_id, "phase": "revision", "kind": "revision",
                           "operation": mapping["operation"], "answer_text": mapping["answer_text"],
                           "evidence_for": assessment["evidence_for"],
                           "covered_targets": assessment["covered_targets"]}
        else:
            assistant_text = mapping["clarification"]
            attribution = {"item_id": old_prompt.get("item_id", "unmapped"), "phase": "revision",
                           "kind": "revision", "operation": "clarify", "evidence_for": [], "covered_targets": []}
        ResponseMessage.objects.create(response_session=locked, sequence=sequence, role="student",
                                       content=student["text"], attribution=attribution)
        ResponseMessage.objects.create(response_session=locked, sequence=sequence + 1, role="assistant",
                                       content=assistant_text,
                                       attribution={"phase": old_prompt["phase"], "item_id": old_prompt.get("item_id")})
        locked.flow_state = state
        locked.turn_version += 1
        locked.next_message_sequence += 2
        locked.save(update_fields=["flow_state", "turn_version", "next_message_sequence", "updated_at"])
    return no_store_json(_session_payload(locked))


@csrf_exempt
def student_turns_view(request, survey_id, session_id):
    if request.method != "POST":
        response = HttpResponseNotAllowed(["POST"])
        response["Cache-Control"] = "no-store"
        return response
    occurrence = _occurrence(survey_id)
    if occurrence is None:
        return no_store_json({"error": "not_found"}, status=404)
    session = _resolve_session(request, occurrence, session_id)
    if session is None:
        return no_store_json({"error": "not_found"}, status=404)
    try:
        student = _json_body(request)
        expected_version = student.pop("expected_version")
        request_id = student.pop("request_id", None)
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id)):
            raise ValueError("invalid request ID")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("invalid turn version")
        kind = student.get("kind")
        required = {"kind"}
        permitted = required | ({"value"} if kind == "rating" else {"text"} if kind == "text" else set())
        permitted.add("item_id")
        if not required <= student.keys() or student.keys() - permitted:
            raise ValueError("invalid turn fields")
        if kind == "text" and (not isinstance(student.get("text"), str)
                               or not 1 <= len(student["text"].strip()) <= 3000):
            raise ValueError("invalid text length")
    except (ValueError, KeyError):
        return no_store_json({"error": "invalid_request"}, status=400)
    from .orchestrated_turns import enabled, process
    if enabled(occurrence.revision.compiled_protocol):
        # Existing clients get deterministic idempotency for an identical version/body retry.
        fallback_id = hashlib.sha256(json.dumps({"version": expected_version, "student": student}, sort_keys=True).encode()).hexdigest()
        return process(request, occurrence, session, student, expected_version, request_id or fallback_id)
    if session.status != "active" or session.turn_version != expected_version:
        return no_store_json({"error": "stale_turn"}, status=409)
    if not _available(occurrence):
        return no_store_json({"error": "survey_closed"}, status=403)
    protocol = occurrence.revision.compiled_protocol
    try:
        prompt = current_prompt(protocol, session.flow_state)
    except (FlowError, ProtocolError, KeyError):
        return no_store_json({"error": "invalid_turn"}, status=422)
    if prompt["phase"] == "complete" and kind != "text":
        return no_store_json({"error": "invalid_turn"}, status=422)
    if prompt["phase"] != "complete" and student.get("item_id") != prompt["item_id"]:
        return no_store_json({"error": "invalid_turn"}, status=422)
    if kind == "text" and (prompt["phase"] == "complete" or _revision_cue(student["text"])):
        revised = _revise_answer(session, occurrence, student, expected_version)
        if revised is not None:
            return revised
    phase = prompt["phase"]
    if kind == "text" and phase in ("answer", "reflection", "probe") and _explicit_decline(student["text"]):
        student["kind"] = "skip"
        kind = "skip"
    assessment = None
    if kind == "text" and phase in ("answer", "reflection", "probe"):
        try:
            assessment = assess_text(protocol, session.flow_state, student["text"])
        except (AssessmentError, OpenAIClientError):
            return no_store_json({"error": "assessment_unavailable", "retryable": True}, status=503)
    clarification_only = bool(assessment and assessment.get("intent") == "clarification")
    try:
        new_state = (deepcopy(session.flow_state) if clarification_only
                     else apply_turn(protocol, session.flow_state, student, assessment=assessment))
    except (FlowError, ProtocolError):
        return no_store_json({"error": "invalid_turn"}, status=422)
    with transaction.atomic():
        locked = ResponseSession.objects.select_for_update().get(pk=session.pk)
        if locked.status != "active" or locked.turn_version != expected_version:
            return no_store_json({"error": "stale_turn"}, status=409)
        old_prompt = current_prompt(protocol, locked.flow_state)
        sequence = locked.next_message_sequence
        content = str(student["value"]) if kind == "rating" else student.get("text", "Skipped this item")
        ResponseMessage.objects.create(
            response_session=locked, sequence=sequence, role="student", content=content,
            attribution={"item_id": old_prompt["item_id"], "phase": old_prompt["phase"],
                         "kind": "clarification" if clarification_only else kind,
                         "rating": student.get("value") if kind == "rating" else None,
                         "evidence_for": assessment["evidence_for"] if assessment else [],
                         "covered_targets": assessment.get("covered_targets", []) if assessment else []},
        )
        if not clarification_only:
            answer_map = new_state.setdefault("answer_map", {})
            answer_map.setdefault(old_prompt["item_id"], []).append(sequence)
            if assessment:
                for linked_id in assessment["evidence_for"]:
                    if linked_id != old_prompt["item_id"]:
                        answer_map.setdefault(linked_id, []).append(sequence)
        next_prompt = current_prompt(protocol, new_state)
        assistant_text = (assessment["clarification_response"] if clarification_only else
                          "All questions are captured. You can revise any answer before downloading your reflection."
                          if next_prompt["phase"] == "complete" else next_prompt["text"])
        ResponseMessage.objects.create(
            response_session=locked, sequence=sequence + 1, role="assistant",
            content=assistant_text,
            attribution={"phase": next_prompt["phase"], "item_id": next_prompt.get("item_id")},
        )
        locked.flow_state = new_state
        locked.turn_version += 1
        locked.next_message_sequence += 2
        changed = ["flow_state", "turn_version", "next_message_sequence", "updated_at"]
        locked.save(update_fields=changed)
    return no_store_json(_session_payload(locked))


@csrf_exempt
def student_finalize_view(request, survey_id, session_id):
    if request.method != "POST":
        response = HttpResponseNotAllowed(["POST"])
        response["Cache-Control"] = "no-store"
        return response
    occurrence = _occurrence(survey_id)
    if occurrence is None:
        return no_store_json({"error": "not_found"}, status=404)
    session = _resolve_session(request, occurrence, session_id)
    if session is None:
        return no_store_json({"error": "not_found"}, status=404)
    try:
        payload = _json_body(request, max_bytes=128)
        expected_version = payload["expected_version"]
        if set(payload) != {"expected_version"} or type(expected_version) is not int or expected_version < 1:
            raise ValueError("invalid finalization version")
    except (ValueError, KeyError):
        return no_store_json({"error": "invalid_request"}, status=400)
    with transaction.atomic():
        locked = ResponseSession.objects.select_for_update().get(pk=session.pk)
        if locked.status != "active" or locked.turn_version != expected_version:
            return no_store_json({"error": "stale_turn"}, status=409)
        if current_prompt(occurrence.revision.compiled_protocol, locked.flow_state)["phase"] != "complete":
            return no_store_json({"error": "not_finished"}, status=422)
        locked.status = "completed"
        locked.completed_at = timezone.now()
        locked.completion_snapshot = {
            "results": deepcopy(locked.flow_state["results"]),
            "answer_map": deepcopy(locked.flow_state.get("answer_map", {})),
            "question_set_revision_id": str(occurrence.revision.public_id),
        }
        locked.turn_version += 1
        locked.save(update_fields=["status", "completed_at", "completion_snapshot", "turn_version", "updated_at"])
    return no_store_json(_session_payload(locked))
