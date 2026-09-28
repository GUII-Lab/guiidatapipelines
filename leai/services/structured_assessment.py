"""Constrain model judgment to one current question and structured evidence IDs."""

import json

from .response_flow import current_prompt


class AssessmentError(ValueError):
    pass


def assess_text(protocol, state, student_text, *, provider=None, force_followup=False):
    if provider is None:
        from datapipeline.openai_client import run_structured
        provider = run_structured
    prompt = current_prompt(protocol, state)
    if prompt["phase"] not in ("answer", "reflection", "probe"):
        raise AssessmentError("text assessment is unavailable in this phase")
    items = [item for section in protocol["sections"] for item in section["items"]]
    current = items[state["item_index"]]
    targets = current.get("coverage_targets", [])
    target_ids = [target["id"] for target in targets]
    future = items[state["item_index"] + 1:]
    allowed_ids = [item["id"] for item in items]
    context = {
        "current_item_id": current["id"],
        "main_question": current["prompt"],
        "current_prompt": prompt["text"],
        "reflection_goal": current["reflection_goal"],
        "required_coverage_targets": targets,
        "optional_probe_examples": current.get("example_probes", []),
        "previous_evidence_for_current": state.get("evidence_seen", {}).get(current["id"], False),
        "future_item_intents": [
            {"id": item["id"], "goal": item["reflection_goal"]} for item in future
        ],
    }
    instructions = (
        "First identify whether the student is answering the exact prompt currently on screen or asking what it "
        "means / asking for an example. If they only request clarification, return intent 'clarification': "
        "explain the current question in plain, student-friendly language, give one or two concrete "
        "examples grounded in this question, and invite them to answer the unchanged question. Do not "
        "treat their clarification request as answer evidence, do not advance, and do not repeat the "
        "question without explaining it. Use current_prompt as the exact wording they may be asking about; "
        "keep main_question unchanged. Put that complete reply in clarification_response; all answer "
        "evidence fields must be empty and sufficient must be false. If they provide substantive answer "
        "content, return intent 'answer' and leave clarification_response empty. You assess an answer to "
        "the current survey item. The validated main question has already been delivered and must not be "
        "rewritten. Decide whether the reflection goal is meaningfully addressed. Optional "
        "probe examples are not mandatory checklist items. A short sincere answer may suffice. "
        "For each required coverage target, report its ID only if the student's text actually covers it. "
        "A bare yes does not cover a target asking how; do not infer missing detail. "
        "If insufficient, propose ONE concise follow-up about only the current item; never ask "
        "the next main question or require every example. Do not praise, grade, or claim a student's "
        "behavior is good or bad. Mark future item IDs in evidence_for only when the student explicitly "
        "addressed them. Return only the specified structured fields. Context: "
        + json.dumps(context, ensure_ascii=False)
    )
    if force_followup:
        instructions += (
            " This is the first substantive answer in an Open conversation. Before moving to the closing "
            "question, ask one concise follow-up grounded in a topic the student actually raised. "
            "Set sufficient to false and put that follow-up in followup. Do not ask the closing question."
        )
    schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": ["answer", "clarification"]},
            "clarification_response": {"type": "string"},
            "sufficient": {"type": "boolean"},
            "followup": {"type": "string"},
            "evidence_for": {"type": "array", "items": {"type": "string", "enum": allowed_ids}},
            "covered_targets": {"type": "array", "items": {"type": "string", **({"enum": target_ids} if target_ids else {})}},
        },
        "required": ["intent", "clarification_response", "sufficient", "followup", "evidence_for", "covered_targets"],
        "additionalProperties": False,
    }
    result = provider([{"role": "system", "content": instructions}], student_text,
                      schema, schema_name="leai_item_assessment")
    parsed = result.get("parsed") if isinstance(result, dict) else None
    if (not isinstance(parsed, dict) or parsed.get("intent") not in ("answer", "clarification")
            or not isinstance(parsed.get("clarification_response"), str)
            or type(parsed.get("sufficient")) is not bool):
        raise AssessmentError("invalid model assessment")
    if parsed["intent"] == "clarification":
        clarification_response = parsed["clarification_response"].strip()
        if (not clarification_response or len(clarification_response) > 1000 or parsed["sufficient"]
                or parsed.get("followup") or parsed.get("evidence_for") or parsed.get("covered_targets")):
            raise AssessmentError("invalid clarification response")
        return {"intent": "clarification", "clarification_response": clarification_response,
                "sufficient": False, "followup": "", "evidence_for": [], "covered_targets": []}
    if parsed["clarification_response"].strip():
        raise AssessmentError("answer assessment must not include a clarification response")
    followup = parsed.get("followup")
    evidence = parsed.get("evidence_for")
    covered = parsed.get("covered_targets", [])
    if (not isinstance(followup, str) or len(followup) > 350
            or not isinstance(evidence, list)
            or any(item_id not in allowed_ids for item_id in evidence)
            or len(evidence) != len(set(evidence))
            or not isinstance(covered, list)
            or any(target_id not in target_ids for target_id in covered)
            or len(covered) != len(set(covered))):
        raise AssessmentError("invalid model assessment fields")
    sufficient = parsed["sufficient"] and set(target_ids) <= set(covered) and not force_followup
    if parsed["sufficient"] and followup.strip() and not force_followup:
        raise AssessmentError("sufficient assessment must not ask another question")
    if force_followup and not followup.strip():
        followup = "Could you tell me more about that experience and what would help?"
    if not sufficient and not followup.strip() and not targets:
        raise AssessmentError("insufficient assessment needs a follow-up")
    if not sufficient and not followup.strip():
        followup = current.get("example_probes", [])[0] if current.get("example_probes") else "Could you say a little more about your answer?"
    if not sufficient and any(
        item["prompt"].strip().casefold() == followup.strip().casefold()
        for item in future
    ):
        raise AssessmentError("follow-up may not repeat a later main question")
    return {"intent": "answer", "clarification_response": "", "sufficient": sufficient, "followup": followup.strip(),
            "evidence_for": evidence, "covered_targets": covered}
