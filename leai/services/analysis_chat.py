"""Private, occurrence-scoped evidence retrieval and leased Chat turns."""
import json

from django.db import transaction
from django.utils import timezone

from datapipeline import openai_client
from leai.models.analysis import AnalysisChatMessage, AnalysisChatSession, AnalysisCitation
from leai.models.jobs import DomainJob
from leai.models.responses import ResponseMessage
from leai.services.jobs import fail_domain_job


TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_message_id": {"type": "integer"},
                    "claim_key": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["source_message_id", "claim_key", "evidence_quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answer", "citations"],
    "additionalProperties": False,
}

DEFAULT_INSTRUCTIONS = (
    "You help an instructor understand anonymous student feedback. Use only the supplied "
    "course feedback evidence. Keep claims appropriately qualified. Cite evidence with an "
    "exact short quote and source message ID. Do not infer student identities. If evidence "
    "does not answer the question, say so plainly."
)


def _turn_sources(job):
    ids = job.payload["occurrence_ids"]
    messages = ResponseMessage.objects.filter(
        response_session__occurrence__course_id=job.course_id,
        response_session__occurrence__public_id__in=ids,
        response_session__status="completed",
        role="student",
    ).select_related("response_session__occurrence").order_by("response_session__occurrence__created_at", "pk")[:200]
    rows = list(messages)
    return rows, [
        {
            "source_message_id": row.pk,
            "occurrence_id": str(row.response_session.occurrence.public_id),
            "occurrence_label": row.response_session.occurrence.label,
            "response": row.content[:1600],
        }
        for row in rows
    ]


def process_feedback_chat_job(job):
    """Run a leased Feedback Chat turn; provider input/output never enters logs or jobs."""
    lease_token = job.lease_token
    try:
        user_message = AnalysisChatMessage.objects.select_related("analysis_chat_session").get(
            pk=int(job.payload["user_message_id"]),
            role="user",
            analysis_chat_session__course_id=job.course_id,
            analysis_chat_session__actor_account_id=job.actor_account_id,
            analysis_chat_session__origin_surface="feedback_chat",
        )
        chat = user_message.analysis_chat_session
        source_rows, evidence = _turn_sources(job)
        history_rows = list(chat.messages.filter(sequence__lt=user_message.sequence, role__in=("user", "assistant")).order_by("sequence"))
        history = [{"role": row.role, "content": row.content} for row in history_rows]
        instructions = chat.prompt_override.strip() if chat.prompt_override and chat.prompt_override.strip() else DEFAULT_INSTRUCTIONS
        history.insert(0, {"role": "system", "content": instructions})
        question = (
            f"Instructor question:\n{user_message.content}\n\n"
            "Available anonymous feedback evidence (IDs are private reference keys; do not show them):\n"
            f"{json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}"
        )
        result = openai_client.run_structured(
            chat_history=history,
            user_text=question,
            json_schema=TURN_SCHEMA,
            schema_name="feedback_chat_turn",
        )
        parsed = result.get("parsed")
        if not isinstance(parsed, dict) or set(parsed) != {"answer", "citations"}:
            raise ValueError("invalid provider response")
        answer = parsed["answer"]
        citations = parsed["citations"]
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 3000 or not isinstance(citations, list) or len(citations) > 10:
            raise ValueError("invalid provider response")
        sources_by_id = {row.pk: row for row in source_rows}
        clean_citations = []
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {"source_message_id", "claim_key", "evidence_quote"}:
                raise ValueError("invalid provider citation")
            source = sources_by_id.get(citation["source_message_id"])
            quote = citation["evidence_quote"]
            claim_key = citation["claim_key"]
            if (source is None or not isinstance(quote, str) or not quote or len(quote) > 1000
                    or quote not in source.content or not isinstance(claim_key, str) or not claim_key.strip() or len(claim_key) > 128):
                raise ValueError("invalid provider citation")
            clean_citations.append((source, claim_key.strip(), quote))

        with transaction.atomic():
            locked_job = DomainJob.objects.select_for_update().filter(
                pk=job.pk, status="running", lease_token=lease_token
            ).first()
            if locked_job is None:
                return False
            locked_chat = AnalysisChatSession.objects.select_for_update().get(pk=chat.pk)
            next_sequence = (locked_chat.messages.order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1
            assistant = AnalysisChatMessage.objects.create(
                analysis_chat_session=locked_chat,
                sequence=next_sequence,
                role="assistant",
                input_method=None,
                content=answer.strip(),
            )
            for source, claim_key, quote in clean_citations:
                AnalysisCitation.objects.create(
                    analysis_chat_message=assistant,
                    response_message=source,
                    claim_key=claim_key,
                    evidence_quote=quote,
                )
            locked_job.status = "completed"
            locked_job.result = {"assistant_message_id": str(assistant.pk)}
            locked_job.error_code = None
            locked_job.lease_token = None
            locked_job.lease_expires_at = None
            locked_job.completed_at = timezone.now()
            locked_job.save(update_fields=("status", "result", "error_code", "lease_token", "lease_expires_at", "completed_at", "updated_at"))
        return True
    except Exception:
        fail_domain_job(job_id=job.pk, lease_token=lease_token, error_code="turn_failed")
        return False
