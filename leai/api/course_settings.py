"""Versioned course-scoped instructor settings."""

import json
import uuid

from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_protect

from leai.models import AuditEvent, Course
from leai.services.actions import has_course_action
from leai.services.instructor_sessions import resolve_instructor_session

from .environment import no_store_json


_BANNER_FIELDS = (
    "banner_enabled",
    "banner_text",
    "banner_dismissible",
    "banner_display_mode",
    "banner_duration_seconds",
    "banner_split_enabled",
    "banner_split_mode",
    "banner_split_value",
)


def _payload(course):
    return {
        **{field: getattr(course, field) for field in _BANNER_FIELDS},
        "settings_version": course.settings_version,
    }


def _valid_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {*_BANNER_FIELDS, "expected_settings_version"}:
        return False
    if type(payload["banner_enabled"]) is not bool or type(payload["banner_dismissible"]) is not bool:
        return False
    if type(payload["banner_split_enabled"]) is not bool:
        return False
    if not isinstance(payload["banner_text"], str) or len(payload["banner_text"]) > 2000:
        return False
    if payload["banner_display_mode"] not in {"persistent", "timed"}:
        return False
    if payload["banner_split_mode"] not in {"percentage", "count"}:
        return False
    if type(payload["banner_duration_seconds"]) is not int or not 1 <= payload["banner_duration_seconds"] <= 600:
        return False
    if type(payload["banner_split_value"]) is not int:
        return False
    if payload["banner_split_mode"] == "percentage":
        if not 0 <= payload["banner_split_value"] <= 100:
            return False
    elif payload["banner_split_value"] < 1:
        return False
    return type(payload["expected_settings_version"]) is int and payload["expected_settings_version"] >= 1


@csrf_protect
def course_banner_settings_view(request, course_id):
    if request.method not in {"GET", "PATCH"}:
        response = HttpResponseNotAllowed(["GET", "PATCH"])
        response["Cache-Control"] = "no-store"
        return response
    session = resolve_instructor_session(request)
    if session is None:
        return no_store_json({"error": "authentication_required"}, status=401)
    course = Course.objects.filter(public_id=course_id, lifecycle_state="active").first()
    if course is None or not has_course_action(session.account, course, "course.manage"):
        return no_store_json({"error": "not_found"}, status=404)
    if request.method == "GET":
        return no_store_json(_payload(course))
    if request.content_type != "application/json" or len(request.body) > 4096:
        return no_store_json({"error": "invalid_request"}, status=400)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return no_store_json({"error": "invalid_request"}, status=400)
    if not _valid_payload(payload):
        return no_store_json({"error": "invalid_request"}, status=400)

    with transaction.atomic():
        locked = Course.objects.select_for_update().filter(pk=course.pk).first()
        if locked is None:
            return no_store_json({"error": "not_found"}, status=404)
        if locked.settings_version != payload["expected_settings_version"]:
            return no_store_json({"error": "settings_conflict", **_payload(locked)}, status=409)
        changed = [field for field in _BANNER_FIELDS if getattr(locked, field) != payload[field]]
        if changed:
            for field in changed:
                setattr(locked, field, payload[field])
            locked.settings_version += 1
            locked.save(update_fields=[*changed, "settings_version"])
            AuditEvent.objects.create(
                actor_account=session.account,
                course=locked,
                actor_kind="platform_admin" if session.account.platform_role == "platform_admin" else "instructor",
                action="course.banner_settings.update",
                outcome="allowed",
                target_type="course",
                target_id=str(locked.public_id),
                request_id=uuid.uuid4().hex,
                bounded_metadata={"changed_fields": changed},
            )
    return no_store_json(_payload(locked))
