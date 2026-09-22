"""Course list and detail with one server-resolved action vocabulary."""

from django.http import HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt

from leai.services.actions import accessible_course_rows
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
    if session.account.must_change_password:
        return None, no_store_json({"error": "password_change_required"}, status=403)
    return session.account, None


@csrf_exempt
def instructor_courses_view(request):
    account, rejection = _course_request_guard(request)
    if rejection is not None:
        return rejection
    return no_store_json({
        "courses": [
            _course_payload(course, actions, role)
            for course, actions, role in accessible_course_rows(account)
        ]
    })


@csrf_exempt
def instructor_course_view(request, course_id):
    account, rejection = _course_request_guard(request)
    if rejection is not None:
        return rejection
    rows = accessible_course_rows(account, course_id=course_id)
    if not rows:
        return no_store_json({"error": "not_found"}, status=404)
    course, actions, role = rows[0]
    return no_store_json(_course_payload(course, actions, role))
