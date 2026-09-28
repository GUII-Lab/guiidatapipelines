"""Synchronous course-scoped read services for the Feedback Analyzer workspace."""

import base64
import json
import math
import re
import uuid
from collections import Counter, defaultdict

from leai.models import ResponseSessionMatchSignal, SurveyOccurrence

from .analysis_corpus import (
    OccurrenceScopeError,
    build_team_summaries,
    eligible_response_records,
    summarize_response_records,
)


_NGRAM_STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because
    been before being below between both but by can could did do does doing down
    during each few for from further get got had has have having he her here hers
    herself him himself his how i if in into is it its itself just like ll me
    might more most my myself no nor not now of off on once only or other our
    ours ourselves out over own re s same she should so some such t than that
    the their theirs them themselves then there these they this those through to
    too under until up ve very was we were what when where which while who whom
    why will with would you your yours yourself yourselves also come done going
    gonna gotta keep let make many may much must need really say said shall since
    still sure take tell thing think try use want way well went work yeah yes""".split()
)
_NGRAM_TOKEN_RE = re.compile(r"[^a-z0-9\s'-]")
_NGRAM_SPACE_RE = re.compile(r"\s+")
_MAX_NGRAM_TERM_LENGTH = 100


def _response_text(record):
    student_texts = [
        str(message.get("content") or "")
        for message in record.get("messages", [])
        if message.get("role") == "student" and message.get("content")
    ]
    if student_texts:
        return " | ".join(student_texts)
    pdf_texts = [
        str(answer.get("value") or "")
        for answer in record.get("pdf_answers", [])
        if answer.get("value")
    ]
    return " | ".join(pdf_texts)


def _normalized_analysis_text(value):
    return _NGRAM_SPACE_RE.sub(" ", _NGRAM_TOKEN_RE.sub(" ", str(value).lower())).strip()


def _tokenize_analysis_text(value):
    return [
        token for token in _normalized_analysis_text(value).split()
        if len(token) > 1 and token not in _NGRAM_STOPWORDS
    ]


def compute_ngrams(records, size):
    """Count legacy-compatible n-grams without crossing response records."""
    if type(size) is not int or size not in {1, 2, 3}:
        raise ValueError("invalid n-gram size")
    counts = {}
    for record in records:
        tokens = _tokenize_analysis_text(_response_text(record))
        for index in range(len(tokens) - size + 1):
            term = " ".join(tokens[index:index + size])
            if len(term) > _MAX_NGRAM_TERM_LENGTH:
                continue
            counts[term] = counts.get(term, 0) + 1
    return [
        {"term": term, "count": count}
        for term, count in sorted(counts.items(), key=lambda item: -item[1])
    ]


def compute_keyness(target_counts, target_total, baseline_counts, baseline_total):
    """Return the legacy log-likelihood keyness score for each target term."""
    combined_total = target_total + baseline_total
    if target_total <= 0 or combined_total <= 0:
        return {}
    scores = {}
    for term, target_count in target_counts.items():
        baseline_count = baseline_counts.get(term, 0)
        total_count = target_count + baseline_count
        expected_target = target_total * total_count / combined_total
        expected_baseline = baseline_total * total_count / combined_total
        score = 0.0
        if target_count > 0 and expected_target > 0:
            score += target_count * math.log(target_count / expected_target)
        if baseline_count > 0 and expected_baseline > 0:
            score += baseline_count * math.log(baseline_count / expected_baseline)
        scores[term] = round(score * 2, 1)
    return scores


def response_matches_term(records, term):
    """Select records containing the normalized whole-token phrase."""
    normalized_term = _normalized_analysis_text(term)
    if not normalized_term:
        return []
    needle = f" {normalized_term} "
    return [
        record for record in records
        if needle in f" {_normalized_analysis_text(_response_text(record))} "
    ]


def ngram_analysis(course, occurrence_ids=None, size=1):
    if type(size) is not int or size not in {1, 2, 3}:
        raise ValueError("invalid n-gram size")

    occurrences_query = SurveyOccurrence.objects.filter(course=course).select_related(
        "revision__question_set"
    ).order_by("created_at", "pk")
    if occurrence_ids is None:
        occurrences = list(occurrences_query)
    else:
        try:
            selected_ids = list(dict.fromkeys(uuid.UUID(str(value)) for value in occurrence_ids))
        except (TypeError, ValueError, AttributeError) as error:
            raise OccurrenceScopeError("invalid occurrence scope") from error
        if not selected_ids:
            occurrences = []
        else:
            found = list(occurrences_query.filter(public_id__in=selected_ids))
            if len(found) != len(selected_ids):
                raise OccurrenceScopeError("occurrence scope is not available in this course")
            found_by_id = {row.public_id: row for row in found}
            occurrences = [found_by_id[value] for value in selected_ids]

    selected_occurrence_ids = [row.public_id for row in occurrences]
    records = eligible_response_records(course, selected_occurrence_ids)
    raw_items = compute_ngrams(records, size)
    keyness_available = False
    keyness_scores = {}
    if len(occurrences) == 1:
        occurrence = occurrences[0]
        baseline_occurrences = SurveyOccurrence.objects.filter(
            course=course,
            revision__question_set_id=occurrence.revision.question_set_id,
        ).exclude(pk=occurrence.pk).order_by("created_at", "pk")
        baseline_ids = list(baseline_occurrences.values_list("public_id", flat=True))
        if baseline_ids:
            baseline_records = eligible_response_records(course, baseline_ids)
            if baseline_records:
                target_counts = {item["term"]: item["count"] for item in raw_items}
                baseline_items = compute_ngrams(baseline_records, size)
                baseline_counts = {item["term"]: item["count"] for item in baseline_items}
                keyness_scores = compute_keyness(
                    target_counts,
                    sum(target_counts.values()),
                    baseline_counts,
                    sum(baseline_counts.values()),
                )
                keyness_available = bool(keyness_scores)

    cutoff = max((record["created_at_sort"] for record in records), default=None)
    return {
        "source_count": len(records),
        "cutoff_at": cutoff.isoformat() if cutoff else None,
        "keyness_available": keyness_available,
        "items": [
            {
                **item,
                "keyness": keyness_scores.get(item["term"]),
            }
            for item in raw_items
        ],
    }


def _mode(occurrence):
    question_set = occurrence.revision.question_set
    if question_set.audience == "team":
        return "team"
    return "structured" if question_set.collection_style == "guided" else "general"


def _summary_by_occurrence(records):
    groups = defaultdict(list)
    for record in records:
        groups[record["occurrence_id"]].append(record)
    return groups


def overview(course, occurrence_ids=None):
    occurrences = SurveyOccurrence.objects.filter(course=course).select_related(
        "revision__question_set"
    ).order_by("created_at", "pk")
    if occurrence_ids is not None:
        try:
            selected = list(dict.fromkeys(str(value) for value in occurrence_ids))
        except TypeError as error:
            raise OccurrenceScopeError("invalid occurrence scope") from error
        if not selected:
            occurrences = occurrences.none()
        else:
            rows = list(occurrences.filter(public_id__in=selected))
            if len(rows) != len(selected):
                raise OccurrenceScopeError("occurrence scope is not available in this course")
            occurrences = occurrences.filter(public_id__in=selected)
    occurrences = list(occurrences)
    selected_ids = [occurrence.public_id for occurrence in occurrences]
    records = eligible_response_records(course, selected_ids)
    summary = summarize_response_records(records)
    per_occurrence = _summary_by_occurrence(records)
    occurrence_rows = []
    for occurrence in occurrences:
        occurrence_records = per_occurrence.get(str(occurrence.public_id), [])
        occurrence_rows.append({
            "id": str(occurrence.public_id),
            "label": occurrence.label,
            "mode": _mode(occurrence),
            "audience": occurrence.revision.question_set.audience,
            "collection_style": occurrence.revision.question_set.collection_style,
            "completion_certificate_enabled": occurrence.completion_certificate_enabled,
            "schema_family_id": str(occurrence.revision.question_set.public_id),
            "created_at": occurrence.created_at.isoformat(),
            "metrics": summarize_response_records(occurrence_records),
        })

    team_summaries = build_team_summaries(records)
    teams_by_snapshot_item = {}
    for row in team_summaries["teams"]:
        teams_by_snapshot_item[row["team_snapshot_item_id"]] = row
    unlinked_by_occurrence = {
        row["occurrence_id"]: row["response_count"]
        for row in team_summaries["unlinked"]
    }
    team_surveys = []
    for occurrence in occurrences:
        if occurrence.revision.question_set.audience != "team":
            continue
        snapshot = getattr(occurrence, "team_snapshot", None)
        configuration_label = (
            snapshot.source_configuration.name if snapshot else "No team snapshot"
        )
        teams = []
        if snapshot:
            for item in snapshot.items.order_by("item_number", "pk"):
                metrics = teams_by_snapshot_item.get(str(item.pk))
                teams.append({
                    "id": str(item.pk),
                    "label": item.label,
                    "response_count": metrics["response_count"] if metrics else 0,
                })
        team_surveys.append({
            "id": str(occurrence.public_id),
            "label": occurrence.label,
            "configuration_label": configuration_label,
            "teams": teams,
            "unlinked_response_count": unlinked_by_occurrence.get(str(occurrence.public_id), 0),
        })
    return {
        "course": {"id": str(course.public_id), "name": course.name},
        "selected_occurrence_ids": [str(value) for value in selected_ids],
        "occurrences": occurrence_rows,
        "summary": summary,
        "team_surveys": team_surveys,
    }


def _response_labels(records):
    counters = defaultdict(int)
    labels = {}
    for record in records:
        counters[record["occurrence_id"]] += 1
        labels[record["id"]] = f"R{counters[record['occurrence_id']]}"
    return labels


def _response_payload(record, label):
    return {
        "kind": "pdf" if record["is_pdf"] else "chat",
        "response_id": record["id"],
        "label": label,
        "survey_label": record["occurrence_label"],
        "created_at": record["created_at"],
        "nudged": any(
            bool(message.get("attribution", {}).get("referred"))
            for message in record["messages"]
        ),
        "response_href": f"#response={record['id']}",
        "transcript": [
            {
                "message_id": str(message["id"]),
                "content": message["content"],
                "timestamp": message["created_at"],
            }
            for message in record["messages"]
            if message["role"] == "student"
        ],
        "answers": [
            {
                "answer_id": answer["answer_id"],
                "question": answer["question"],
                "value": answer["value"],
            }
            for answer in record["pdf_answers"]
        ],
        "occurrence_id": record["occurrence_id"],
        "team_snapshot_id": record["team_snapshot_id"],
        "team_snapshot_item_id": record["team_snapshot_item_id"],
        "team_label": record["team_label"],
        "team_configuration_label": record["team_configuration_label"],
        "source": record["source"],
    }


def _decode_cursor(cursor):
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        offset = int(decoded)
    except (ValueError, UnicodeDecodeError, base64.binascii.Error) as error:
        raise ValueError("invalid cursor") from error
    if offset < 0 or offset > 100000:
        raise ValueError("invalid cursor")
    return offset


def response_page(
    course,
    occurrence_ids=None,
    *,
    cursor=None,
    limit=25,
    source="all",
    nudged_only=False,
    term=None,
    team_snapshot_item_id=None,
    unlinked_occurrence_id=None,
):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid page limit")
    if source not in {"all", "chat", "pdf"}:
        raise ValueError("invalid response source")
    if term is not None and (not isinstance(term, str) or not 1 <= len(term.strip()) <= 100):
        raise ValueError("invalid response term")
    offset = _decode_cursor(cursor)
    records = eligible_response_records(course, occurrence_ids)
    if team_snapshot_item_id is not None:
        records = [row for row in records if row["team_snapshot_item_id"] == str(team_snapshot_item_id)]
    if unlinked_occurrence_id is not None:
        records = [
            row for row in records
            if row["audience"] == "team"
            and row["occurrence_id"] == str(unlinked_occurrence_id)
            and row["team_snapshot_item_id"] is None
        ]
    if source != "all":
        records = [row for row in records if row["source"] == ("pdf" if source == "pdf" else "student")]
    labels = _response_labels(records)
    rows = [_response_payload(record, labels[record["id"]]) for record in records]
    if nudged_only:
        rows = [row for row in rows if row["nudged"]]
    if term is not None:
        matching_ids = {
            row["id"] for row in response_matches_term(records, term)
        }
        rows = [row for row in rows if row["response_id"] in matching_ids]
    rows.reverse()
    page = rows[offset:offset + limit]
    has_more = offset + limit < len(rows)
    next_cursor = None
    if has_more:
        next_cursor = base64.urlsafe_b64encode(
            str(offset + limit).encode("ascii")
        ).decode("ascii").rstrip("=")
    return {"results": page, "has_more": has_more, "next_cursor": next_cursor}


def response_detail(course, response_id):
    records = eligible_response_records(course)
    record = next((row for row in records if row["id"] == str(response_id)), None)
    if record is None:
        return None
    labels = _response_labels(records)
    return _response_payload(record, labels[record["id"]])


def progress(course, occurrence_ids=None):
    occurrences = SurveyOccurrence.objects.filter(course=course).select_related(
        "revision__question_set"
    ).order_by("created_at", "pk")
    if occurrence_ids is not None:
        try:
            selected = list(dict.fromkeys(str(value) for value in occurrence_ids))
        except TypeError as error:
            raise OccurrenceScopeError("invalid occurrence scope") from error
        if not selected:
            occurrences = occurrences.none()
        else:
            rows = list(occurrences.filter(public_id__in=selected))
            if len(rows) != len(selected):
                raise OccurrenceScopeError("occurrence scope is not available in this course")
            occurrences = occurrences.filter(public_id__in=selected)
    occurrences = list(occurrences)
    records = eligible_response_records(course, [row.public_id for row in occurrences])
    occurrence_rows = [{"id": str(row.public_id), "label": row.label} for row in occurrences]
    if not course.anonymous_matching_enabled:
        return {
            "state": "unavailable",
            "reason": "matching_disabled",
            "occurrences": occurrence_rows,
            "students": [],
            "groups": [],
            "unlinked_by_occurrence": [],
        }

    signal_by_session = {
        signal.response_session_id: signal
        for signal in ResponseSessionMatchSignal.objects.filter(
            response_session_id__in=[record["_session_pk"] for record in records]
        )
    }
    fingerprint_devices = defaultdict(set)
    for signal in signal_by_session.values():
        if signal.fingerprint_digest and signal.device_key_digest:
            fingerprint_devices[signal.fingerprint_digest].add(signal.device_key_digest)

    identity_groups = defaultdict(list)
    for record in records:
        signal = signal_by_session.get(record["_session_pk"])
        if signal is None:
            continue
        if signal.device_key_digest:
            group_key = ("device", signal.device_key_digest)
        elif signal.fingerprint_digest:
            devices = fingerprint_devices.get(signal.fingerprint_digest, set())
            if len(devices) > 1:
                continue
            group_key = ("device", next(iter(devices))) if devices else ("fingerprint", signal.fingerprint_digest)
        else:
            continue
        identity_groups[group_key].append((record, "high" if signal.device_key_digest else "moderate"))

    linked_groups = [
        members for members in identity_groups.values()
        if len({member[0]["occurrence_id"] for member in members}) >= 2
    ]
    linked_groups.sort(key=lambda members: min(member[0]["created_at_sort"] for member in members))
    students = []
    label_by_session = {}
    for index, members in enumerate(linked_groups, start=1):
        label = f"S{index}"
        counts = Counter(member[0]["occurrence_id"] for member in members)
        confidence = (
            "high" if all(member_confidence == "high" for _, member_confidence in members)
            else "moderate"
        )
        students.append({
            "label": label,
            "match_confidence": confidence,
            "responses_by_occurrence": dict(counts),
        })
        for record, _ in members:
            label_by_session[record["_session_pk"]] = label

    team_groups = {}
    unlinked = Counter()
    for record in records:
        if record["audience"] != "team":
            continue
        if not record["team_snapshot_id"] or not record["team_snapshot_item_id"]:
            unlinked[record["occurrence_id"]] += 1
            continue
        if record["_session_pk"] not in label_by_session:
            continue
        key = (record["team_configuration_id"], record["team_stable_key"])
        team = team_groups.setdefault(key, {
            "configuration_id": record["team_configuration_id"],
            "configuration_label": record["team_configuration_label"],
            "team_label": record["team_label"],
            "snapshot_ids": set(),
            "first_seen": record["created_at_sort"],
            "responses_by_occurrence": Counter(),
        })
        team["snapshot_ids"].add(record["team_snapshot_id"])
        team["first_seen"] = min(team["first_seen"], record["created_at_sort"])
        team["responses_by_occurrence"][record["occurrence_id"]] += 1
    eligible_teams = [
        team for team in team_groups.values()
        if len(team["responses_by_occurrence"]) >= 2
    ]
    eligible_teams.sort(key=lambda team: team["first_seen"])
    groups = [
        {
            "label": f"G{index}",
            "team_snapshot_id": ",".join(sorted(team["snapshot_ids"], key=int)),
            "team_snapshot_label": f"{team['configuration_label']} · {team['team_label']}",
            "responses_by_occurrence": dict(team["responses_by_occurrence"]),
        }
        for index, team in enumerate(eligible_teams, start=1)
    ]
    return {
        "state": "available",
        "occurrences": occurrence_rows,
        "students": students,
        "groups": groups,
        "unlinked_by_occurrence": [
            {"occurrence_id": key, "response_count": unlinked[key]}
            for key in sorted(unlinked)
        ],
    }
