"""Small, course-scoped search of completed student feedback."""

import json
import re

from django.http import HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_protect

from leai.models import Course, ResponseMessage
from leai.services.actions import has_course_action
from leai.services.instructor_sessions import resolve_instructor_session

from .environment import no_store_json


RESULT_LIMIT = 20
EXCERPT_LIMIT = 240


def _excerpt(content, query):
    match = re.search(re.escape(query), content, flags=re.IGNORECASE)
    hit = match.start() if match else -1
    start = max(0, hit - 40) if hit >= 0 else 0
    allowance = EXCERPT_LIMIT - int(start > 0)
    end = min(len(content), start + allowance)
    excerpt = content[start:end]
    if start:
        excerpt = "…" + excerpt
    if end < len(content):
        excerpt = excerpt[: EXCERPT_LIMIT - 1] + "…"
    return excerpt


@csrf_protect
def response_search_view(request, course_id):
    if request.method != "POST":
        response = HttpResponseNotAllowed(["POST"])
        response["Cache-Control"] = "no-store"
        return response

    session = resolve_instructor_session(request)
    if session is None:
        return no_store_json({"error": "authentication_required"}, status=401)
    account = session.account

    course = Course.objects.filter(public_id=course_id, lifecycle_state="active").first()
    if course is None or not has_course_action(account, course, "responses.view"):
        return no_store_json({"error": "not_found"}, status=404)

    if request.content_type != "application/json" or len(request.body) > 512:
        return no_store_json({"error": "invalid_request"}, status=400)
    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return no_store_json({"error": "invalid_request"}, status=400)
    if not isinstance(payload, dict) or set(payload) != {"query"} or not isinstance(payload["query"], str):
        return no_store_json({"error": "invalid_request"}, status=400)
    query = payload["query"].strip()
    if not 2 <= len(query) <= 100:
        return no_store_json({"error": "invalid_request"}, status=400)

    matches = list(
        ResponseMessage.objects.filter(
            response_session__occurrence__course=course,
            response_session__status="completed",
            role="student",
            content__icontains=query,
        )
        .select_related("response_session__occurrence")
        .order_by("-created_at", "-pk")[: RESULT_LIMIT + 1]
    )
    results = [
        {
            "message_id": message.pk,
            "response_id": str(message.response_session.public_id),
            "occurrence_label": message.response_session.occurrence.label,
            "excerpt": _excerpt(message.content, query),
            "created_at": message.created_at.isoformat(),
        }
        for message in matches[:RESULT_LIMIT]
    ]
    return no_store_json({
        "query": query,
        "results": results,
        "has_more": len(matches) > RESULT_LIMIT,
    })
