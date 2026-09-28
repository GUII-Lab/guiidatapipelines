"""Course-scoped synchronous Feedback Analyzer reads and anonymous matching settings."""

import hashlib
import hmac
import json
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect

from datapipeline.leai_completion import normalize_code
from leai.models import AuditEvent, Course, ResponseSession, ResponseSessionMatchSignal, SurveyOccurrence
from leai.services.actions import has_course_action
from leai.services.feedback_analyzer import (
    OccurrenceScopeError,
    ngram_analysis,
    overview,
    progress,
    response_detail,
    response_page,
)
from leai.services.instructor_sessions import resolve_instructor_session

from .environment import no_store_json
from .student_responses import _resolve_session


_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_SIGNAL_VALUE = re.compile(r"^[\x21-\x7e]+$")


def _method_not_allowed(methods):
    response = HttpResponseNotAllowed(methods)
    response["Cache-Control"] = "no-store"
    return response


def _course_request(request, course_id, action):
    session = resolve_instructor_session(request)
    if session is None:
        return None, None, no_store_json({"error": "authentication_required"}, status=401)
    course = Course.objects.filter(
        public_id=course_id,
        lifecycle_state="active",
    ).first()
    if course is None or not has_course_action(session.account, course, action):
        return None, None, no_store_json({"error": "not_found"}, status=404)
    return course, session.account, None


def _occurrence_ids(request):
    values = request.GET.getlist("occurrence_ids")
    if not values:
        return None
    if len(values) != 1 or len(values[0]) > 2000:
        raise ValueError("invalid occurrence scope")
    parts = [value for value in values[0].split(",") if value]
    if len(parts) > 50 or any(not _UUID.fullmatch(value) for value in parts):
        raise ValueError("invalid occurrence scope")
    return parts


def _scope_error(error):
    if isinstance(error, OccurrenceScopeError):
        return no_store_json({"error": "invalid_occurrence_scope"}, status=400)
    return no_store_json({"error": "invalid_request"}, status=400)


@csrf_protect
def analysis_overview_view(request, course_id):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    course, _, rejection = _course_request(request, course_id, "analysis.use")
    if rejection is not None:
        return rejection
    try:
        occurrence_ids = _occurrence_ids(request)
        data = overview(course, occurrence_ids)
    except (ValueError, OccurrenceScopeError) as error:
        return _scope_error(error)
    return no_store_json(data)


@csrf_protect
def analysis_ngrams_view(request, course_id):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    course, _, rejection = _course_request(request, course_id, "analysis.use")
    if rejection is not None:
        return rejection
    try:
        occurrence_ids = _occurrence_ids(request)
        size_text = request.GET.get("size", "1")
        if size_text not in {"1", "2", "3"} or set(request.GET.keys()) - {"occurrence_ids", "size"}:
            raise ValueError("invalid n-gram request")
        data = ngram_analysis(course, occurrence_ids, int(size_text))
    except (ValueError, OccurrenceScopeError) as error:
        return _scope_error(error)
    return no_store_json(data)


@csrf_protect
def analysis_responses_view(request, course_id):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    course, _, rejection = _course_request(request, course_id, "responses.view")
    if rejection is not None:
        return rejection
    try:
        occurrence_ids = _occurrence_ids(request)
        limit_text = request.GET.get("limit", "25")
        if not re.fullmatch(r"[0-9]{1,3}", limit_text):
            raise ValueError("invalid page limit")
        limit = int(limit_text)
        nudged_text = request.GET.get("nudged_only", "false")
        if nudged_text not in {"true", "false"}:
            raise ValueError("invalid nudge filter")
        allowed = {
            "occurrence_ids", "limit", "cursor", "source", "nudged_only",
            "team_snapshot_item_id", "unlinked_occurrence_id", "term",
        }
        if set(request.GET.keys()) - allowed:
            raise ValueError("unsupported query parameter")
        data = response_page(
            course,
            occurrence_ids,
            cursor=request.GET.get("cursor"),
            limit=limit,
            source=request.GET.get("source", "all"),
            nudged_only=nudged_text == "true",
            term=request.GET.get("term"),
            team_snapshot_item_id=request.GET.get("team_snapshot_item_id"),
            unlinked_occurrence_id=request.GET.get("unlinked_occurrence_id"),
        )
    except (ValueError, OccurrenceScopeError) as error:
        return _scope_error(error)
    return no_store_json(data)


@csrf_protect
def analysis_response_detail_view(request, course_id, response_id):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    course, _, rejection = _course_request(request, course_id, "responses.view")
    if rejection is not None:
        return rejection
    response = response_detail(course, response_id)
    if response is None:
        return no_store_json({"error": "not_found"}, status=404)
    return no_store_json(response)


@csrf_protect
def analysis_certificate_verify_view(request, course_id):
    if request.method != "POST":
        return _method_not_allowed(["POST"])

    if request.content_type != "application/json" or len(request.body) > 8192:
        return no_store_json({"error": "invalid_request"}, status=400)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return no_store_json({"error": "invalid_request"}, status=400)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"occurrence_id", "codes"}
        or not isinstance(payload["occurrence_id"], str)
        or not isinstance(payload["codes"], list)
        or len(payload["codes"]) > 100
        or any(not isinstance(code, str) or len(code) > 64 for code in payload["codes"])
    ):
        return no_store_json({"error": "invalid_request"}, status=400)
    try:
        occurrence_id = uuid.UUID(payload["occurrence_id"])
    except (ValueError, AttributeError):
        return no_store_json({"error": "invalid_request"}, status=400)

    course, _, rejection = _course_request(request, course_id, "responses.view")
    if rejection is not None:
        return rejection
    occurrence = SurveyOccurrence.objects.filter(
        public_id=occurrence_id,
        course=course,
    ).only("id", "completion_certificate_enabled").first()
    if occurrence is None:
        return no_store_json({"error": "not_found"}, status=404)
    if not occurrence.completion_certificate_enabled:
        return no_store_json({"error": "certificates_disabled"}, status=403)

    normalized_codes = []
    lookup_codes = set()
    for raw_code in payload["codes"]:
        try:
            code = normalize_code(raw_code)
        except ValueError:
            code = None
        if code is not None:
            lookup_codes.add(code)
        normalized_codes.append(code)

    matching_codes = set()
    if lookup_codes:
        matching_codes = set(
            ResponseSession.objects.filter(
                occurrence=occurrence,
                certificate_code__in=lookup_codes,
            ).values_list("certificate_code", flat=True)
        )
    return no_store_json({
        "results": [code in matching_codes if code is not None else False for code in normalized_codes],
    })


@csrf_protect
def analysis_progress_view(request, course_id):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    course, _, rejection = _course_request(request, course_id, "analysis.use")
    if rejection is not None:
        return rejection
    try:
        data = progress(course, _occurrence_ids(request))
    except (ValueError, OccurrenceScopeError) as error:
        return _scope_error(error)
    return no_store_json(data)


def _settings_payload(course):
    return {
        "anonymous_matching_enabled": course.anonymous_matching_enabled,
        "settings_version": course.settings_version,
    }


@csrf_protect
def analysis_settings_view(request, course_id):
    if request.method not in {"GET", "PATCH"}:
        return _method_not_allowed(["GET", "PATCH"])
    course, actor, rejection = _course_request(request, course_id, "course.manage")
    if rejection is not None:
        return rejection
    if request.method == "GET":
        return no_store_json(_settings_payload(course))
    if request.content_type != "application/json" or len(request.body) > 256:
        return no_store_json({"error": "invalid_request"}, status=400)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return no_store_json({"error": "invalid_request"}, status=400)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"anonymous_matching_enabled", "expected_settings_version"}
        or type(payload["anonymous_matching_enabled"]) is not bool
        or type(payload["expected_settings_version"]) is not int
        or payload["expected_settings_version"] < 1
    ):
        return no_store_json({"error": "invalid_request"}, status=400)
    with transaction.atomic():
        locked = Course.objects.select_for_update().filter(pk=course.pk).first()
        if locked is None:
            return no_store_json({"error": "not_found"}, status=404)
        if locked.settings_version != payload["expected_settings_version"]:
            return no_store_json({
                "error": "settings_conflict",
                **_settings_payload(locked),
            }, status=409)
        enabled = payload["anonymous_matching_enabled"]
        if locked.anonymous_matching_enabled != enabled:
            locked.anonymous_matching_enabled = enabled
            locked.settings_version += 1
            locked.save(update_fields=["anonymous_matching_enabled", "settings_version"])
            AuditEvent.objects.create(
                actor_account=actor,
                course=locked,
                actor_kind=(
                    "platform_admin"
                    if actor.platform_role == "platform_admin"
                    else "instructor"
                ),
                action="course.anonymous_matching.update",
                outcome="allowed",
                target_type="course",
                target_id=str(locked.public_id),
                request_id=uuid.uuid4().hex,
                bounded_metadata={"changed_fields": ["anonymous_matching_enabled"]},
            )
    return no_store_json(_settings_payload(locked))


def _matching_digest(value, signal_kind):
    environment = getattr(settings, "LEAI_ENVIRONMENT", "local") or "local"
    secret = getattr(settings, "LEAI_MATCHING_HMAC_KEY", "") or settings.SECRET_KEY
    environment_key = hmac.new(
        secret.encode("utf-8"),
        f"leai-anonymous-matching:{environment}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return hmac.new(
        environment_key,
        f"{signal_kind}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@csrf_exempt
def matching_signals_view(request, survey_id, session_id):
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    occurrence = SurveyOccurrence.objects.select_related("course").filter(
        public_id=survey_id,
    ).first()
    if occurrence is None:
        return no_store_json({"error": "not_found"}, status=404)
    session = _resolve_session(request, occurrence, session_id)
    if session is None:
        return no_store_json({"error": "not_found"}, status=404)
    if not occurrence.course.anonymous_matching_enabled:
        return no_store_json({"accepted": False})
    if request.content_type != "application/json" or len(request.body) > 1024:
        return no_store_json({"error": "invalid_request"}, status=400)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return no_store_json({"error": "invalid_request"}, status=400)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"device_key", "fingerprint"}
        or not isinstance(payload["device_key"], str)
        or not isinstance(payload["fingerprint"], str)
    ):
        return no_store_json({"error": "invalid_request"}, status=400)
    device_key = payload["device_key"].strip()
    fingerprint = payload["fingerprint"].strip()
    if (
        len(device_key) > 128
        or len(fingerprint) > 256
        or (device_key and not _SIGNAL_VALUE.fullmatch(device_key))
        or (fingerprint and not _SIGNAL_VALUE.fullmatch(fingerprint))
    ):
        return no_store_json({"error": "invalid_request"}, status=400)
    if not device_key and not fingerprint:
        return no_store_json({"accepted": False})
    with transaction.atomic():
        locked_course = Course.objects.select_for_update().filter(pk=occurrence.course_id).first()
        locked_session = ResponseSession.objects.select_for_update().filter(
            pk=session.pk,
            occurrence=occurrence,
            source="student",
        ).first()
        if locked_course is None or locked_session is None:
            return no_store_json({"error": "not_found"}, status=404)
        if not locked_course.anonymous_matching_enabled:
            return no_store_json({"accepted": False})
        ResponseSessionMatchSignal.objects.update_or_create(
            response_session=locked_session,
            defaults={
                "device_key_digest": _matching_digest(device_key, "device") if device_key else None,
                "fingerprint_digest": _matching_digest(fingerprint, "fingerprint") if fingerprint else None,
            },
        )
    return no_store_json({"accepted": True})
