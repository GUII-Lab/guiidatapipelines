"""Contextual conversation proposals. Pure validation, no database or provider access."""
from copy import deepcopy
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .protocol import validate_protocol


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(Contract):
    sequence: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=3000)


class Target(Contract):
    id: str
    status: Literal["covered", "missing", "not_applicable"]


class AnswerUpdate(Contract):
    item_id: str
    status: Literal["answered", "partial", "declined", "unknown", "not_applicable"]
    evidence: list[Evidence] = Field(min_length=1, max_length=30)
    supersede_sequences: list[int] = Field(max_length=30)
    targets: list[Target] = Field(max_length=30)


class TurnProposal(Contract):
    updates: list[AnswerUpdate] = Field(max_length=40)
    action: Literal["ask_main", "follow_up", "clarify", "review_ready"]
    item_id: str | None
    reply: str = Field(max_length=1600)


def items(protocol):
    return [item for section in protocol["sections"] for item in section["items"]]


def overlaps(left, right):
    return (left['sequence'] == right['sequence']
            and left['start'] < right['end'] and right['start'] < left['end'])


def retired_parts(ref, retained):
    """Subtract explicitly retained excerpts; preserve unrelated parts of one message."""
    parts = [ref]
    for keep in retained:
        remaining = []
        for part in parts:
            if not overlaps(part, keep):
                remaining.append(part)
                continue
            if part['start'] < keep['start']:
                remaining.append({**part, 'end': keep['start']})
            if keep['end'] < part['end']:
                remaining.append({**part, 'start': keep['end']})
        parts = remaining
    return parts


def preview_proposal(protocol, state, messages, raw):
    """Validate referenced evidence and lifecycle. Does NOT prove semantic entailment.

    Quotes are exact substrings; persisted spans use Python Unicode code points.
    Ambiguous repeated substrings are rejected instead of guessing offsets.
    """
    validate_protocol(protocol)
    p = TurnProposal.model_validate(raw)
    ordered = items(protocol)
    by_id = {item["id"]: item for item in ordered}
    if any(item["response"]["kind"] != "text" for item in ordered):
        raise ValueError("Option 1 executor currently supports text protocols only")
    if any(re.search(r"(?<!\w)" + re.escape(qid) + r"(?!\w)", p.reply, re.I) for qid in by_id):
        raise ValueError("student reply exposes internal question identifiers")
    if p.action in ("clarify", "follow_up") and not p.reply.strip():
        raise ValueError("clarification/follow-up needs student-facing text")
    updates = {u.item_id: u for u in p.updates}
    if len(updates) != len(p.updates) or updates.keys() - by_id.keys():
        raise ValueError("duplicate or unknown answer update")
    message_by_seq = {m["sequence"]: m for m in messages if m["role"] == "student"}
    next_state = deepcopy(state)
    answer_map = next_state.setdefault("answer_map", {})
    evidence = next_state.setdefault("evidence", {})
    # Existing message-level maps must not silently disappear from excerpt exports.
    for qid, sequences in answer_map.items():
        refs = evidence.setdefault(qid, [])
        represented = {ref['sequence'] for ref in refs}
        for seq in sequences:
            if seq not in represented:
                message = message_by_seq.get(seq)
                if message is None:
                    raise ValueError('existing evidence does not reference a student message')
                quote = message.get('attribution', {}).get('answer_text', message['content'])
                if not isinstance(quote, str) or not quote or message['content'].count(quote) != 1:
                    raise ValueError('existing extracted answer cannot be represented faithfully as source evidence')
                start = message['content'].index(quote)
                refs.append({'sequence': seq, 'start': start, 'end': start + len(quote)})
    retired = next_state.setdefault('superseded_evidence', {})
    for u in p.updates:
        prior = answer_map.get(u.item_id, [])
        if len(set(u.supersede_sequences)) != len(u.supersede_sequences) or set(u.supersede_sequences) - set(prior):
            raise ValueError("supersession must refer to existing evidence for this question")
        refs = []
        for ref in u.evidence:
            message = message_by_seq.get(ref.sequence)
            if message is None or message["content"].count(ref.quote) != 1:
                raise ValueError("evidence quote must uniquely match a student message in this session")
            start = message["content"].index(ref.quote)
            refs.append({"sequence": ref.sequence, "start": start, "end": start + len(ref.quote)})
        targets = {t.id: t.status for t in u.targets}
        expected = {t["id"] for t in by_id[u.item_id].get("coverage_targets", [])}
        if len(targets) != len(u.targets) or targets.keys() - expected:
            raise ValueError("unknown or duplicate coverage target")
        if u.status == "answered" and any(targets.get(t) not in ("covered", "not_applicable") for t in expected):
            raise ValueError("answered question has unresolved applicable targets")
        old_refs = evidence.get(u.item_id, [])
        removed = []
        for seq in u.supersede_sequences:
            parts = [part for r in old_refs if r['sequence'] == seq for part in retired_parts(r, refs)]
            if not parts:
                raise ValueError('supersession must actually retire existing evidence')
            removed.extend(parts)
        retired.setdefault(u.item_id, []).extend(removed)
        evidence[u.item_id] = [r for r in old_refs if r["sequence"] not in u.supersede_sequences]
        evidence[u.item_id].extend(r for r in refs if r not in evidence[u.item_id])
        answer_map[u.item_id] = sorted(set(prior) - set(u.supersede_sequences) | {r.sequence for r in u.evidence})
        result = next_state["results"].setdefault(u.item_id, {"rating": None, "probes": 0})
        result.update(status=u.status, targets=targets)
        next_state.setdefault("coverage_seen", {})[u.item_id] = [t for t, status in targets.items() if status == "covered"]
        next_state.setdefault("evidence_seen", {})[u.item_id] = True

    # Check the resulting graph, not just whether every affected question was named.
    # A withdrawn claim cannot survive under another question or be revived later.
    all_retired = [ref for refs in retired.values() for ref in refs]
    if any(overlaps(active, old) for refs in evidence.values() for active in refs for old in all_retired):
        raise ValueError('active evidence overlaps a retired claim; reassess every affected question')

    # Questions stay in authored order, even when a student answered later ones early.
    old_index = state["item_index"]
    presented = next_state.setdefault("presented_main_ids", [q["id"] for q in ordered[:min(old_index + 1, len(ordered))]])
    def closed(qid):
        result = next_state["results"].get(qid, {})
        return (result.get("status") in ("answered", "declined", "unknown", "not_applicable")
                or (result.get("status") == "partial" and result.get("probes", 0) >= by_id[qid].get("max_additional_probes", 2)))

    if p.action == "review_ready":
        if p.item_id is not None or any(q["id"] not in presented or not closed(q["id"]) for q in ordered):
            raise ValueError("cannot complete unpresented or unresolved questions")
        next_state.update(item_index=len(ordered), phase="complete", pending_prompt=None)
    else:
        if p.item_id not in by_id:
            raise ValueError("unknown next question")
        index = next(i for i, q in enumerate(ordered) if q["id"] == p.item_id)
        if p.action == "ask_main":
            if any(q["id"] not in presented or not closed(q["id"]) for q in ordered[:index]):
                raise ValueError("cannot skip unresolved or unpresented earlier questions")
            if p.item_id not in presented:
                presented.append(p.item_id)
            next_state.update(item_index=index, phase="answer", pending_prompt=None)
        else:
            if p.item_id not in presented:
                raise ValueError("clarify/follow-up only applies to presented questions")
            if p.action == "follow_up":
                result = next_state["results"].setdefault(p.item_id, {"rating": None, "status": "partial", "probes": 0})
                if closed(p.item_id) or result.get("probes", 0) >= by_id[p.item_id].get("max_additional_probes", 2):
                    raise ValueError("cannot probe a closed question or exceed its budget")
                result["probes"] = result.get("probes", 0) + 1
                next_state.update(item_index=index, phase="probe", pending_prompt=p.reply.strip())
            else:
                # Keep the actual clarification text as the next turn's conversational context.
                if index != old_index or state["phase"] == "complete":
                    next_state.update(item_index=index, phase="answer", pending_prompt=None)
                else:
                    next_state.update(item_index=index)
    next_state["last_dialogue_action"] = p.action
    reply = p.reply.strip()
    if p.action == "ask_main":
        stem = by_id[p.item_id]["prompt"]
        # The authored stem has one owner. Strip only exact trailing copies;
        # an embedded copy needs provider repair rather than rewriting its context.
        while reply.endswith(stem):
            reply = reply[:-len(stem)].rstrip()
        if stem in reply:
            raise ValueError("main question belongs in the renderer, not inside the bridge")
        reply = (reply + "\n\n" + stem).strip()
    if not reply:
        raise ValueError("empty student response")
    next_state["last_assistant_text"] = reply
    return next_state, reply
