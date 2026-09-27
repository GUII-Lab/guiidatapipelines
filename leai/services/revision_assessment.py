"""Locate a student's requested answer change without sending old answers to the model."""

import json

from .structured_assessment import AssessmentError


def classify_revision(protocol, text, *, current_item_id=None, provider=None):
    if provider is None:
        from datapipeline.openai_client import run_structured
        provider = run_structured
    items = [item for section in protocol["sections"] for item in section["items"]]
    ids = [item["id"] for item in items]
    context = [{"id": item["id"], "question": item["prompt"], "goal": item["reflection_goal"]}
               for item in items]
    instructions = (
        "Determine whether this message is a request to edit an earlier survey answer. "
        "If it is simply answering the current question, return operation 'answer' and empty "
        "item_id, answer_text, and clarification. Identify ONE question for an actual edit. "
        "Use only the new student message; no previous student messages are available. "
        "For 'replace', answer_text is only the new content that supersedes their earlier answer. "
        "For 'add', answer_text is only the new detail to append. Preserve the student's meaning; "
        "If they explicitly revise a Likert rating, set rating to the new numeric value; otherwise null. "
        "never invent or expand facts. If you cannot confidently determine a target question or "
        "there is no substantive new answer content, return operation 'clarify', an empty item_id "
        "and answer_text, and a concise clarification question. Return only the specified fields. "
        f"Current question ID: {current_item_id or 'none, all questions have been asked'}. "
        "Survey questions: " + json.dumps(context, ensure_ascii=False)
    )
    schema = {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "enum": ["", *ids]},
            "operation": {"type": "string", "enum": ["replace", "add", "clarify", "answer"]},
            "answer_text": {"type": "string"},
            "clarification": {"type": "string"},
            "rating": {"type": ["integer", "null"]},
        },
        "required": ["item_id", "operation", "answer_text", "clarification", "rating"],
        "additionalProperties": False,
    }
    result = provider([{"role": "system", "content": instructions}], text,
                      schema, schema_name="leai_answer_revision")
    parsed = result.get("parsed") if isinstance(result, dict) else None
    if not isinstance(parsed, dict):
        raise AssessmentError("invalid revision mapping")
    item_id = parsed.get("item_id")
    operation = parsed.get("operation")
    answer_text = parsed.get("answer_text")
    clarification = parsed.get("clarification")
    rating = parsed.get("rating")
    if (item_id not in ["", *ids] or operation not in ("replace", "add", "clarify", "answer")
            or not isinstance(answer_text, str) or len(answer_text) > 3000
            or not isinstance(clarification, str) or len(clarification) > 350
            or (rating is not None and type(rating) is not int)):
        raise AssessmentError("invalid revision mapping fields")
    target = next((item for item in items if item["id"] == item_id), None)
    if rating is not None and (target is None or target["response"]["kind"] != "likert"
                               or rating not in {choice["value"] for choice in protocol["scales"][target["response"]["scale_id"]]}):
        raise AssessmentError("invalid revised rating")
    if operation == "answer":
        if item_id or answer_text.strip() or clarification.strip() or rating is not None:
            raise AssessmentError("invalid ordinary-answer classification")
    elif operation == "clarify":
        if item_id or answer_text.strip() or not clarification.strip():
            raise AssessmentError("invalid revision clarification")
    elif not item_id or not answer_text.strip() or clarification.strip():
        raise AssessmentError("revision needs a target and new answer content")
    return {"item_id": item_id, "operation": operation, "rating": rating,
            "answer_text": answer_text.strip(), "clarification": clarification.strip()}
