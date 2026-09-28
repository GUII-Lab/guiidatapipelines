"""Course-authorized Feedback Chat API backed by canonical response records."""
import hashlib
import json
import re

from django.http import HttpResponse, HttpResponseNotAllowed
from django.db import transaction
from django.views.decorators.csrf import csrf_protect

from leai.models.analysis import AnalysisChatMessage, AnalysisChatSession, AnalysisCitation, AnalysisScopeOccurrence
from leai.models.authoring import SurveyOccurrence
from leai.models.identity import Course
from leai.models.jobs import DomainJob
from leai.services.actions import has_course_action
from leai.services.instructor_sessions import resolve_instructor_session
from leai.services.jobs import enqueue_domain_job, public_job_status, start_domain_job_thread
from leai.services.mutation_receipts import IdempotencyConflict, execute_once
from .environment import no_store_json


MAX_BODY_BYTES = 8192
TITLE_LIMIT = 120
PROMPT_LIMIT = 4000
TURN_LIMIT = 3000
ID_PATTERN = re.compile(r"^[1-9][0-9]{0,19}$")


class ActiveChatTurnConflict(Exception):
    """A prior user turn in this Chat is still being processed."""


def _not_found():
    return no_store_json({"error": "not_found"}, status=404)


def _method_not_allowed(methods):
    response = HttpResponseNotAllowed(methods)
    response["Cache-Control"] = "no-store"
    return response


def _context(request, course_id):
    session = resolve_instructor_session(request)
    if session is None:
        return None, None, no_store_json({"error": "authentication_required"}, status=401)
    course = Course.objects.filter(public_id=course_id, lifecycle_state="active").first()
    if course is None or not has_course_action(session.account, course, "analysis.use"):
        return None, None, _not_found()
    return session.account, course, None


def _body(request, exact_keys, *, optional_keys=()):
    if request.content_type != "application/json" or len(request.body) > MAX_BODY_BYTES:
        return None
    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) - set(exact_keys) - set(optional_keys) or not set(exact_keys).issubset(payload):
        return None
    return payload


def _chat(course, actor, chat_id, *, writable=False):
    query = AnalysisChatSession.objects.filter(
        public_id=chat_id,
        course=course,
        actor_account=actor,
        origin_surface="feedback_chat",
    )
    if writable:
        query = query.filter(archived=False)
    return query.first()


def _source_dto(occurrence):
    return {
        "id": str(occurrence.public_id),
        "label": occurrence.label,
        "revision": occurrence.revision.revision_number,
    }


def _message_dto(message):
    citations = []
    for index, citation in enumerate(message.citations.select_related("response_message__response_session__occurrence__revision", "response_session__occurrence__revision").order_by("pk"), start=1):
        source_message = citation.response_message
        source_session = citation.response_session if citation.response_session_id else source_message.response_session
        occurrence = source_session.occurrence
        citations.append({
            "id": str(citation.pk),
            "citation_number": index,
            "claim_key": citation.claim_key,
            "response_id": str(source_session.public_id),
            "response_message_id": source_message.pk if source_message else None,
            "occurrence_id": str(occurrence.public_id),
            "week_label": None,
            "survey_label": occurrence.label,
            "question_label": None,
            "evidence_quote": citation.evidence_quote,
        })
    return {
        "id": str(message.pk),
        "sequence": message.sequence,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "citations": citations,
    }


def _chat_detail(chat):
    sources = [row.survey_occurrence for row in chat.occurrence_scope.select_related("survey_occurrence__revision").order_by("survey_occurrence__created_at", "pk")]
    messages = chat.messages.exclude(role="system").prefetch_related("citations__response_message__response_session__occurrence", "citations__response_session__occurrence").order_by("sequence")
    return {
        "id": str(chat.public_id),
        "title": chat.title,
        "prompt_override": chat.prompt_override,
        "archived": chat.archived,
        "updated_at": chat.updated_at.isoformat(),
        "sources": [_source_dto(source) for source in sources],
        "messages": [_message_dto(message) for message in messages],
    }


def _occurrence_ids(payload):
    values = payload.get("occurrence_ids")
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        return None
    if any(not isinstance(value, str) for value in values) or len(values) != len(set(values)):
        return None
    try:
        return [str(SurveyOccurrence._meta.get_field("public_id").to_python(value)) for value in values]
    except (ValueError, TypeError):
        return None


@csrf_protect
def feedback_chat_occurrences_view(request, course_id):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    rows = SurveyOccurrence.objects.filter(course=course).select_related("revision").order_by("created_at", "pk")
    return no_store_json({"occurrences": [_source_dto(row) for row in rows[:200]]})


@csrf_protect
def feedback_chats_view(request, course_id):
    if request.method not in ("GET", "POST"):
        return _method_not_allowed(["GET", "POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    if request.method == "GET":
        chats = AnalysisChatSession.objects.filter(course=course, actor_account=actor, origin_surface="feedback_chat", archived=False).order_by("-updated_at", "-pk")[:200]
        return no_store_json({"chats": [{"id": str(chat.public_id), "title": chat.title, "updated_at": chat.updated_at.isoformat()} for chat in chats]})
    payload = _body(request, set(), optional_keys={"title"})
    if payload is None:
        return no_store_json({"error": "invalid_request"}, status=400)
    title = payload.get("title", "New chat")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_LIMIT:
        return no_store_json({"error": "invalid_request"}, status=400)
    chat = AnalysisChatSession.objects.create(course=course, actor_account=actor, origin_surface="feedback_chat", title=title.strip())
    return no_store_json(_chat_detail(chat), status=201)


@csrf_protect
def feedback_chat_detail_view(request, course_id, chat_id):
    if request.method not in ("GET", "PATCH", "DELETE"):
        return _method_not_allowed(["GET", "PATCH", "DELETE"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    chat = _chat(course, actor, chat_id, writable=request.method != "GET")
    if chat is None:
        return _not_found()
    if request.method == "GET":
        return no_store_json(_chat_detail(chat))
    if request.method == "DELETE":
        chat.archived = True
        chat.save(update_fields=("archived", "updated_at"))
        return HttpResponse(status=204, headers={"Cache-Control": "no-store"})
    payload = _body(request, set(), optional_keys={"title", "prompt_override"})
    if payload is None or not payload or ("title" in payload and (not isinstance(payload["title"], str) or not payload["title"].strip() or len(payload["title"].strip()) > TITLE_LIMIT)) or ("prompt_override" in payload and payload["prompt_override"] is not None and (not isinstance(payload["prompt_override"], str) or len(payload["prompt_override"]) > PROMPT_LIMIT)):
        return no_store_json({"error": "invalid_request"}, status=400)
    fields = []
    if "title" in payload:
        chat.title = payload["title"].strip()
        fields.append("title")
    if "prompt_override" in payload:
        chat.prompt_override = payload["prompt_override"]
        fields.append("prompt_override")
    fields.append("updated_at")
    chat.save(update_fields=fields)
    return no_store_json(_chat_detail(chat))


@csrf_protect
def feedback_chat_scope_view(request, course_id, chat_id):
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    chat = _chat(course, actor, chat_id, writable=True)
    if chat is None:
        return _not_found()
    payload = _body(request, {"occurrence_ids"})
    ids = _occurrence_ids(payload) if payload else None
    if ids is None:
        return no_store_json({"error": "invalid_request"}, status=400)
    with transaction.atomic():
        # Serialize scope additions with turn enqueue. The queued job captures
        # the exact append-only scope visible while this session lock is held.
        chat = AnalysisChatSession.objects.select_for_update().filter(
            pk=chat.pk, course=course, actor_account=actor, origin_surface="feedback_chat", archived=False
        ).first()
        if chat is None:
            return _not_found()
        occurrences = list(SurveyOccurrence.objects.filter(course=course, public_id__in=ids))
        if len(occurrences) != len(ids):
            return _not_found()
        for occurrence in occurrences:
            AnalysisScopeOccurrence.objects.get_or_create(analysis_chat_session=chat, survey_occurrence=occurrence)
        chat.save(update_fields=("updated_at",))
        detail = _chat_detail(chat)
    return no_store_json(detail)


@csrf_protect
def feedback_chat_turn_view(request, course_id, chat_id):
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    chat = _chat(course, actor, chat_id, writable=True)
    if chat is None:
        return _not_found()
    payload = _body(request, {"content"}, optional_keys={"retry_message_id"})
    content = payload.get("content", "").strip() if payload and isinstance(payload.get("content"), str) else ""
    retry_id = payload.get("retry_message_id") if payload else None
    if not 1 <= len(content) <= TURN_LIMIT or ("retry_message_id" in payload and (not isinstance(retry_id, str) or not ID_PATTERN.fullmatch(retry_id))):
        return no_store_json({"error": "invalid_request"}, status=400)
    idempotency_key = request.headers.get("Idempotency-Key", "")
    if not idempotency_key or len(idempotency_key) > 1024:
        return no_store_json({"error": "idempotency_key_required"}, status=400)
    canonical_request = {"content": content}
    if retry_id:
        canonical_request["retry_message_id"] = retry_id
    request_hash = hashlib.sha256(json.dumps(canonical_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def mutate():
        with transaction.atomic():
            locked_chat = AnalysisChatSession.objects.select_for_update().get(pk=chat.pk, archived=False)
            has_messages = locked_chat.messages.exists()
            latest_message = locked_chat.messages.order_by("-sequence").first()
            if latest_message and latest_message.role == "user" and DomainJob.objects.filter(
                course=course,
                actor_account=actor,
                job_type="feedback_chat_turn",
                status__in=("pending", "running"),
                payload__user_message_id=str(latest_message.pk),
            ).exists():
                raise ActiveChatTurnConflict
            if retry_id:
                user_message = locked_chat.messages.filter(pk=int(retry_id), role="user", content=content).first()
                if user_message is None:
                    raise ValueError("retry message does not belong to this chat")
            else:
                sequence = (locked_chat.messages.order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1
                user_message = AnalysisChatMessage.objects.create(analysis_chat_session=locked_chat, sequence=sequence, role="user", input_method="typed", content=content)
                if not has_messages and locked_chat.title == "New chat":
                    locked_chat.title = content[:TITLE_LIMIT]
                    locked_chat.save(update_fields=("title", "updated_at"))
            occurrence_ids = list(locked_chat.occurrence_scope.select_related("survey_occurrence").order_by("survey_occurrence__created_at", "pk").values_list("survey_occurrence__public_id", flat=True))
            if not occurrence_ids:
                raise ValueError("add at least one feedback source before sending a turn")
            job = enqueue_domain_job(job_type="feedback_chat_turn", course=course, actor=actor, payload={"user_message_id": str(user_message.pk), "occurrence_ids": [str(value) for value in occurrence_ids]})
            return {"job_id": str(job.public_id)}

    try:
        result, replayed = execute_once(
            principal_scope=f"instructor-account:{actor.pk}",
            operation="feedback_chat_turn",
            target_key=str(chat.public_id),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            mutate=mutate,
        )
    except IdempotencyConflict:
        return no_store_json({"error": "idempotency_conflict"}, status=409)
    except ActiveChatTurnConflict:
        return no_store_json({"error": "turn_in_progress"}, status=409)
    except ValueError:
        return no_store_json({"error": "invalid_request"}, status=400)
    transaction.on_commit(lambda: start_domain_job_thread(result["job_id"]))
    return no_store_json(result, status=202)


@csrf_protect
def feedback_chat_job_view(request, course_id, job_id):
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    actor, course, error = _context(request, course_id)
    if error:
        return error
    job = DomainJob.objects.filter(public_id=job_id, course=course, actor_account=actor, job_type="feedback_chat_turn").first()
    if job is None:
        return _not_found()
    if job.status == "pending" or (
        job.status == "running"
        and job.lease_expires_at is not None
        and job.lease_expires_at <= timezone.now()
    ):
        transaction.on_commit(lambda: start_domain_job_thread(str(job.public_id)))
    return no_store_json(public_job_status(job))
