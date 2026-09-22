"""Small, course-scoped search of completed student feedback."""

from django.http import HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt

from leai.models import Course, ResponseMessage
from leai.services.actions import has_course_action
from leai.services.instructor_sessions import resolve_instructor_session

from .environment import no_store_json


RESULT_LIMIT = 20
EXCERPT_LIMIT = 240


def _excerpt(content, query):
    hit = content.casefold().find(query.casefold())
    start = max(0, hit - 40) if hit >= 0 else 0
    allowance = EXCERPT_LIMIT - int(start > 0)
    end = min(len(content), start + allowance)
    excerpt = content[start:end]
    if start:
        excerpt = "…" + excerpt
    if end < len(content):
        excerpt = excerpt[: EXCERPT_LIMIT - 1] + "…"
    return excerpt


@csrf_exempt
def response_search_view(request, course_id):
    if request.method != "GET":
        response = HttpResponseNotAllowed(["GET"])
        response["Cache-Control"] = "no-store"
        return response

    session = resolve_instructor_session(request)
    if session is None:
        return no_store_json({"error": "authentication_required"}, status=401)
    account = session.account
    if account.must_change_password:
        return no_store_json({"error": "password_change_required"}, status=403)

    course = Course.objects.filter(public_id=course_id, lifecycle_state="active").first()
    if course is None or not has_course_action(account, course, "responses.view"):
        return no_store_json({"error": "not_found"}, status=404)

    if set(request.GET) != {"q"} or len(request.GET.getlist("q")) != 1:
        return no_store_json({"error": "invalid_request"}, status=400)
    query = request.GET["q"].strip()
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
