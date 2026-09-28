"""Course-scoped response population and exact-schema metrics for Feedback Analyzer."""

import re
import uuid
from collections import Counter

from leai.models import ResponseSession, SurveyOccurrence


class OccurrenceScopeError(ValueError):
    """A requested occurrence is invalid or does not belong to the selected course."""


UNAVAILABLE_DENOMINATOR = {
    "state": "unavailable",
    "reason": "eligible_denominator_missing",
}


def _requested_occurrences(course, occurrence_ids):
    occurrences = SurveyOccurrence.objects.filter(course=course).select_related(
        "revision__question_set"
    )
    if occurrence_ids is None:
        return list(occurrences.order_by("created_at", "pk"))

    try:
        requested = list(dict.fromkeys(uuid.UUID(str(value)) for value in occurrence_ids))
    except (TypeError, ValueError, AttributeError) as error:
        raise OccurrenceScopeError("invalid occurrence scope") from error
    if not requested:
        return []
    rows = list(occurrences.filter(public_id__in=requested).order_by("created_at", "pk"))
    if len(rows) != len(requested):
        raise OccurrenceScopeError("occurrence scope is not available in this course")
    return rows


def _pdf_question_answer(content):
    match = re.match(r"^Q:\s*([\s\S]*?)\n\nA:\s*([\s\S]*)$", content)
    if match:
        return match.group(1).strip() or "Imported question", match.group(2).strip()
    return "Imported response", content.strip()


def _pdf_answer(content):
    return _pdf_question_answer(content)[1]


def _message_payload(message, *, pdf=False):
    return {
        "id": message.pk,
        "role": "student" if pdf else message.role,
        "content": _pdf_answer(message.content) if pdf else message.content,
        "created_at": message.created_at.isoformat(),
        "attribution": message.attribution if not pdf else {},
    }


def eligible_response_records(course, occurrence_ids=None):
    """Return only response-bearing sessions scoped to this course and occurrences.

    Student sessions become responses after a persisted student turn. A committed
    PDF session is one response regardless of its stored message/answer count.
    """
    occurrences = _requested_occurrences(course, occurrence_ids)
    if not occurrences:
        return []
    occurrence_by_pk = {row.pk: row for row in occurrences}
    sessions = (
        ResponseSession.objects.filter(occurrence_id__in=occurrence_by_pk)
        .select_related(
            "occurrence__revision__question_set",
            "pdf_import_batch",
            "team_snapshot_item__snapshot__source_configuration",
        )
        .prefetch_related("messages")
        .order_by("created_at", "pk")
    )
    records = []
    for session in sessions:
        occurrence = occurrence_by_pk[session.occurrence_id]
        messages = list(session.messages.all())
        if session.source == "student":
            student_messages = [message for message in messages if message.role == "student"]
            if not student_messages:
                continue
            display_messages = [
                _message_payload(message)
                for message in messages
                if message.role in {"student", "assistant"}
            ]
            word_texts = [message.content for message in student_messages]
            turn_count = len(student_messages)
            is_pdf = False
            pdf_answers = []
        elif session.source == "pdf":
            if session.pdf_import_batch is None or session.pdf_import_batch.status not in {
                "committed", "completed",
            }:
                continue
            pdf_messages = [message for message in messages if message.role != "system"]
            pdf_answers = [
                {
                    "answer_id": str(message.pk),
                    "question": _pdf_question_answer(message.content)[0],
                    "value": _pdf_question_answer(message.content)[1],
                }
                for message in pdf_messages
                if _pdf_answer(message.content)
            ]
            word_texts = [answer["value"] for answer in pdf_answers]
            display_messages = []
            turn_count = 0
            is_pdf = True
        else:
            continue

        item = session.team_snapshot_item
        snapshot = item.snapshot if item else None
        question_set = occurrence.revision.question_set
        records.append({
            "id": str(session.public_id),
            "created_at": session.created_at.isoformat(),
            "created_at_sort": session.created_at,
            "occurrence_id": str(occurrence.public_id),
            "occurrence_label": occurrence.label,
            "audience": question_set.audience,
            "collection_style": question_set.collection_style,
            "schema_family_id": str(question_set.public_id),
            "source": session.source,
            "is_pdf": is_pdf,
            "student_turn_count": turn_count,
            "word_count": sum(len(text.split()) for text in word_texts),
            "messages": display_messages,
            "pdf_answers": pdf_answers,
            "team_snapshot_id": str(snapshot.pk) if snapshot else None,
            "team_snapshot_item_id": str(item.pk) if item else None,
            "team_label": item.label if item else None,
            "team_configuration_id": str(snapshot.source_configuration_id) if snapshot else None,
            "team_configuration_label": snapshot.source_configuration.name if snapshot else None,
            "team_stable_key": item.stable_key if item else None,
            "_session_pk": session.pk,
            "_protocol": occurrence.revision.compiled_protocol,
            "_occurrence_pk": occurrence.pk,
        })
    return records


def _unavailable(reason):
    return {"state": "unavailable", "reason": reason}


def _question_health(records):
    if not records:
        return _unavailable("no_response_records")
    if len({record["schema_family_id"] for record in records}) > 1:
        return _unavailable("mixed_schema_families")

    protocols = [record["_protocol"] for record in records]
    def identifier_shape(protocol):
        sections = protocol.get("sections") if isinstance(protocol, dict) else None
        if not isinstance(sections, list):
            return None
        shape = []
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get("id"), str):
                return None
            items = section.get("items")
            if not isinstance(items, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                for item in items
            ):
                return None
            shape.append((section["id"], tuple(item["id"] for item in items)))
        return tuple(shape)
    shapes = [identifier_shape(protocol) for protocol in protocols]
    if any(shape is None for shape in shapes):
        return _unavailable("exact_structured_identifiers_unavailable")
    if len(set(shapes)) != 1:
        return _unavailable("incompatible_schema_revisions")

    protocol = protocols[0]
    sections = protocol.get("sections") if isinstance(protocol, dict) else None
    if not isinstance(sections, list) or not sections:
        return _unavailable("exact_structured_identifiers_unavailable")
    normalized_sections = []
    allowed_items = {}
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("id"), str):
            return _unavailable("exact_structured_identifiers_unavailable")
        items = section.get("items")
        if not isinstance(items, list) or not items:
            return _unavailable("exact_structured_identifiers_unavailable")
        normalized_items = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                return _unavailable("exact_structured_identifiers_unavailable")
            question_id = item["id"]
            if question_id in allowed_items:
                return _unavailable("exact_structured_identifiers_unavailable")
            allowed_items[question_id] = section["id"]
            normalized_items.append({
                "question_id": question_id,
                "prompt": item.get("prompt") or item.get("wording") or question_id,
            })
        normalized_sections.append({
            "section_id": section["id"],
            "title": section.get("title") or section["id"],
            "questions": normalized_items,
        })

    answered_by_question = {question_id: set() for question_id in allowed_items}
    for record in records:
        for message in record["messages"]:
            item_id = message.get("attribution", {}).get("item_id")
            if item_id in answered_by_question:
                answered_by_question[item_id].add(record["id"])
    output = []
    for section in normalized_sections:
        question_rows = []
        section_respondents = set()
        for question in section["questions"]:
            respondent_ids = answered_by_question[question["question_id"]]
            section_respondents.update(respondent_ids)
            question_rows.append({
                **question,
                "response_count": len(respondent_ids),
            })
        output.append({
            **section,
            "response_count": len(section_respondents),
            "questions": question_rows,
        })
    return {"state": "available", "sections": output}


def _turn_distribution(records):
    if not records:
        return _unavailable("no_response_records")
    if len({record["schema_family_id"] for record in records}) != 1:
        return _unavailable("mixed_schema_families")
    if len({record["_occurrence_pk"] for record in records}) < 2:
        return _unavailable("insufficient_occurrences")
    counts = Counter(record["student_turn_count"] for record in records)
    return [
        {"student_turn_count": turn_count, "response_count": counts[turn_count]}
        for turn_count in sorted(counts)
    ]


def summarize_response_records(records):
    response_count = len(records)
    return {
        "response_count": response_count,
        "student_turn_count": sum(record["student_turn_count"] for record in records),
        "pdf_response_count": sum(record["is_pdf"] for record in records),
        "average_words": (
            round(sum(record["word_count"] for record in records) / response_count, 1)
            if response_count else None
        ),
        "participation": dict(UNAVAILABLE_DENOMINATOR),
        "turn_distribution": _turn_distribution(records),
        "question_health": _question_health(records),
    }


def build_team_summaries(records):
    """Aggregate immutable TeamSnapshotItem rows and preserve unlinked responses."""
    teams = {}
    unlinked = Counter()
    for record in records:
        if record["audience"] != "team":
            continue
        if not record["team_snapshot_id"] or not record["team_snapshot_item_id"]:
            unlinked[record["occurrence_id"]] += 1
            continue
        key = (record["team_snapshot_id"], record["team_snapshot_item_id"])
        team = teams.setdefault(key, {
            "team_snapshot_id": record["team_snapshot_id"],
            "team_snapshot_item_id": record["team_snapshot_item_id"],
            "team_configuration_id": record["team_configuration_id"],
            "team_configuration_label": record["team_configuration_label"],
            "team_label": record["team_label"],
            "response_count": 0,
            "occurrence_ids": [],
        })
        team["response_count"] += 1
        if record["occurrence_id"] not in team["occurrence_ids"]:
            team["occurrence_ids"].append(record["occurrence_id"])
    return {
        "teams": sorted(
            teams.values(),
            key=lambda row: (int(row["team_snapshot_id"]), int(row["team_snapshot_item_id"])),
        ),
        "unlinked": [
            {"occurrence_id": occurrence_id, "response_count": unlinked[occurrence_id]}
            for occurrence_id in sorted(unlinked)
        ],
    }
