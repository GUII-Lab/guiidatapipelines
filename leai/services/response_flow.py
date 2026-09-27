"""Deterministic question order around model-assessed free-text responses."""

from copy import deepcopy

from .protocol import validate_protocol


class FlowError(ValueError):
    pass


def _items(protocol):
    return [item for section in protocol["sections"] for item in section["items"]]


def begin_flow(protocol):
    validate_protocol(protocol)
    return {
        "item_index": 0,
        "phase": "rating" if _items(protocol)[0]["response"]["kind"] == "likert" else "answer",
        "pending_prompt": None,
        "results": {},
        "answer_map": {},
        "evidence_seen": {},
        "coverage_seen": {},
    }


def current_prompt(protocol, state):
    items = _items(protocol)
    index = state["item_index"]
    if index == len(items):
        return {"phase": "complete"}
    if not 0 <= index < len(items):
        raise FlowError("invalid item cursor")
    item = items[index]
    phase = state["phase"]
    text = state["pending_prompt"] if phase in ("reflection", "probe") else item["prompt"]
    response = item["response"]
    prompt = {
        "item_id": item["id"],
        "phase": phase,
        "text": text,
        "wording": item["wording"] if phase in ("rating", "answer") else "adaptive",
        "choices": protocol["scales"][response["scale_id"]] if phase == "rating" else None,
    }
    if state.get("evidence_seen", {}).get(item["id"]) and phase in ("rating", "answer"):
        prompt["context_note"] = "You may have touched on this earlier. Please confirm or clarify your answer here."
    return prompt


def advance_flow(protocol, state):
    next_index = state["item_index"] + 1
    resume_index = state.pop("resume_item_index", None)
    if resume_index is not None and next_index >= resume_index:
        next_index = resume_index
    state["item_index"] = next_index
    state["pending_prompt"] = None
    items = _items(protocol)
    if state["item_index"] == len(items):
        state["phase"] = "complete"
    else:
        state["phase"] = "rating" if items[state["item_index"]]["response"]["kind"] == "likert" else "answer"


def apply_turn(protocol, state, student, *, assessment=None):
    """Return a new state; callers persist it only with the student turn."""
    validate_protocol(protocol)
    next_state = deepcopy(state)
    prompt = current_prompt(protocol, next_state)
    if prompt["phase"] == "complete" or student.get("item_id") != prompt["item_id"]:
        raise FlowError("response does not match the current item")
    item = _items(protocol)[next_state["item_index"]]
    result = next_state["results"].setdefault(item["id"], {"rating": None, "status": "active", "probes": 0})
    kind = student.get("kind")
    if kind == "skip":
        result["status"] = "declined"
        advance_flow(protocol, next_state)
        return next_state

    if prompt["phase"] == "rating":
        values = {choice["value"] for choice in prompt["choices"]}
        if kind != "rating" or type(student.get("value")) is not int or student["value"] not in values:
            raise FlowError("an explicit valid rating is required")
        result["rating"] = student["value"]
        label = next(choice["label"] for choice in prompt["choices"] if choice["value"] == student["value"])
        next_state["phase"] = "reflection"
        next_state["pending_prompt"] = f"You selected {label}. What led you to choose that rating?"
        return next_state

    text = student.get("text")
    if kind != "text" or not isinstance(text, str) or not text.strip():
        raise FlowError("a nonempty text response is required")
    if not isinstance(assessment, dict) or type(assessment.get("sufficient")) is not bool:
        raise FlowError("a structured sufficiency assessment is required")
    evidence_for = assessment.get("evidence_for", [])
    valid_ids = {candidate["id"] for candidate in _items(protocol)}
    if not isinstance(evidence_for, list) or not all(candidate in valid_ids for candidate in evidence_for):
        raise FlowError("assessment refers to an unknown item")
    for candidate in evidence_for:
        next_state["evidence_seen"][candidate] = True

    required_targets = {target["id"] for target in item.get("coverage_targets", [])}
    covered_targets = assessment.get("covered_targets", [])
    if (not isinstance(covered_targets, list) or not all(isinstance(target, str) for target in covered_targets)
            or len(covered_targets) != len(set(covered_targets))
            or any(target not in required_targets for target in covered_targets)):
        raise FlowError("assessment refers to an unknown coverage target")
    coverage = next_state.setdefault("coverage_seen", {}).setdefault(item["id"], [])
    for target in covered_targets:
        if target not in coverage:
            coverage.append(target)
    sufficient = required_targets <= set(coverage) if required_targets else assessment["sufficient"]
    if sufficient:
        result["status"] = "answered"
        advance_flow(protocol, next_state)
        return next_state
    limit = item.get("max_additional_probes", 2)
    if result["probes"] >= limit:
        result["status"] = "partial"
        advance_flow(protocol, next_state)
        return next_state
    followup = assessment.get("followup")
    if not isinstance(followup, str) or not followup.strip():
        raise FlowError("insufficient response requires a follow-up question")
    if prompt["phase"] == "probe" and followup.strip().casefold() == prompt["text"].strip().casefold():
        result["status"] = "partial"
        advance_flow(protocol, next_state)
        return next_state
    result["probes"] += 1
    next_state["phase"] = "probe"
    next_state["pending_prompt"] = followup.strip()
    return next_state
