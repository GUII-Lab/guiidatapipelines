"""Researcher-controlled course debug visibility for QA student conversations."""

import json

from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt, csrf_protect

from leai.models import Course, SurveyOccurrence
from leai.services.actions import has_researcher_course_access
from leai.services.instructor_sessions import resolve_instructor_session

from .environment import no_store_json


def _researcher(request, course):
    session = resolve_instructor_session(request)
    if session is None:
        return None, no_store_json({"error": "authentication_required"}, status=401)
    if not has_researcher_course_access(session.account, course):
        return None, no_store_json({"error": "not_found"}, status=404)
    return session.account, None


def _qa_only():
    from django.conf import settings
    return settings.LEAI_ENVIRONMENT in ("local", "qa")


@csrf_protect
def research_debug_settings_view(request, course_id):
    if request.method not in ("GET", "PATCH"):
        response = HttpResponseNotAllowed(["GET", "PATCH"])
        response["Cache-Control"] = "no-store"
        return response
    if not _qa_only():
        return no_store_json({"error": "not_found"}, status=404)
    course = Course.objects.filter(public_id=course_id, lifecycle_state="active").first()
    if course is None:
        return no_store_json({"error": "not_found"}, status=404)
    _, rejection = _researcher(request, course)
    if rejection is not None:
        return rejection
    if request.method == "GET":
        return no_store_json({
            "debug_enabled": course.student_debug_enabled,
            "settings_version": course.settings_version,
        })

    if request.content_type != "application/json" or len(request.body) > 256:
        return no_store_json({"error": "invalid_request"}, status=400)
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return no_store_json({"error": "invalid_request"}, status=400)
    if (not isinstance(payload, dict)
            or set(payload) != {"debug_enabled", "expected_settings_version"}
            or type(payload["debug_enabled"]) is not bool
            or type(payload["expected_settings_version"]) is not int
            or payload["expected_settings_version"] < 1):
        return no_store_json({"error": "invalid_request"}, status=400)

    with transaction.atomic():
        locked = Course.objects.select_for_update().filter(pk=course.pk).first()
        if locked is None:
            return no_store_json({"error": "not_found"}, status=404)
        if locked.settings_version != payload["expected_settings_version"]:
            return no_store_json({"error": "settings_conflict"}, status=409)
        if locked.student_debug_enabled != payload["debug_enabled"]:
            locked.student_debug_enabled = payload["debug_enabled"]
            locked.settings_version += 1
            locked.save(update_fields=["student_debug_enabled", "settings_version"])
    return no_store_json({
        "debug_enabled": locked.student_debug_enabled,
        "settings_version": locked.settings_version,
    })


@csrf_exempt
def student_debug_access_view(request, survey_id):
    if request.method != "GET":
        response = HttpResponseNotAllowed(["GET"])
        response["Cache-Control"] = "no-store"
        return response
    if not _qa_only():
        return no_store_json({"error": "not_found"}, status=404)
    occurrence = SurveyOccurrence.objects.select_related("course").filter(public_id=survey_id).first()
    if occurrence is None:
        return no_store_json({"error": "not_found"}, status=404)
    _, rejection = _researcher(request, occurrence.course)
    if rejection is not None:
        return rejection
    return no_store_json({"enabled": occurrence.course.student_debug_enabled})
