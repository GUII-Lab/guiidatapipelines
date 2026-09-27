"""Course list, creation and detail with one server-resolved action vocabulary."""

import json
import re
import uuid

from django.db import IntegrityError, transaction
from django.http import HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_protect

from leai.models import AuditEvent, Course, CourseMembership, Institution, InstitutionMembership
from leai.services.actions import accessible_course_rows, allowed_course_actions
from leai.services.instructor_sessions import resolve_instructor_session

from .environment import no_store_json


def _course_payload(course, actions, role):
    return {
        "course_id": str(course.public_id),
        "course_code": course.course_code,
        "course_name": course.name,
        "institution_slug": course.institution.slug,
        "lifecycle_state": course.lifecycle_state,
        "role": role,
        "allowed_actions": list(actions),
    }


def _course_request_guard(request):
    if request.method != "GET":
        response = HttpResponseNotAllowed(["GET"])
        response["Cache-Control"] = "no-store"
        return None, response
    session = resolve_instructor_session(request)
    if session is None:
        return None, no_store_json({"error": "authentication_required"}, status=401)
    return session.account, None


@csrf_protect
def instructor_courses_view(request):
    if request.method == "POST":
        return _create_course(request)
    account, rejection = _course_request_guard(request)
    if rejection is not None:
        return rejection
    return no_store_json({
        "courses": [
            _course_payload(course, actions, role)
            for course, actions, role in accessible_course_rows(account)
        ]
    })


def _course_create_body(request):
    if request.content_type != "application/json" or len(request.body) > 8192:
        return None
    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"institution_slug", "course_code", "course_name"}:
        return None
    if not all(isinstance(value, str) for value in payload.values()):
        return None
    slug, code, name = (payload[key].strip() for key in ("institution_slug", "course_code", "course_name"))
    if not re.fullmatch(r"[a-z0-9-]{1,64}", slug) or not re.fullmatch(r"[a-z0-9-]{1,100}", code):
        return None
    if not 1 <= len(name) <= 200:
        return None
    return slug, code, name


def _create_course(request):
    session = resolve_instructor_session(request)
    if session is None:
        return no_store_json({"error": "authentication_required"}, status=401)
    fields = _course_create_body(request)
    if fields is None:
        return no_store_json({"error": "invalid_request"}, status=400)
    slug, code, name = fields
    account = session.account
    try:
        with transaction.atomic():
            membership = InstitutionMembership.objects.select_for_update().select_related("institution").filter(
                account=account, institution__slug=slug, role="instructor", is_active=True,
            ).first()
            if membership is None:
                return no_store_json({"error": "forbidden"}, status=403)
            # Serialize new courses under one institution, including duplicate-code checks.
            institution = Institution.objects.select_for_update().get(pk=membership.institution_id)
            if Course.objects.filter(institution=institution, course_code=code).exists():
                return no_store_json({"error": "course_code_taken"}, status=409)
            course = Course.objects.create(institution=institution, course_code=code, name=name)
            CourseMembership.objects.create(course=course, institution_membership=membership, role="owner")
            AuditEvent.objects.create(
                actor_account=account,
                course=course,
                actor_kind="platform_admin" if account.platform_role == "platform_admin" else "instructor",
                action="course.create",
                outcome="allowed",
                target_type="course",
                target_id=str(course.public_id),
                request_id=uuid.uuid4().hex,
                bounded_metadata={},
            )
    except IntegrityError:
        return no_store_json({"error": "course_code_taken"}, status=409)
    return no_store_json(_course_payload(course, allowed_course_actions(account, course), "owner"), status=201)


@csrf_protect
def instructor_course_view(request, course_id):
    account, rejection = _course_request_guard(request)
    if rejection is not None:
        return rejection
    rows = accessible_course_rows(account, course_id=course_id)
    if not rows:
        return no_store_json({"error": "not_found"}, status=404)
    course, actions, role = rows[0]
    return no_store_json(_course_payload(course, actions, role))
