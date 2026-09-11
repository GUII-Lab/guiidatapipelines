from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.db import transaction

from .models import FeedbackGPT, FeedbackMessage, ResponseSession


FORM_ATTRIBUTION_FIELDS = (
    'form_schema_id',
    'form_schema_version',
    'form_section_id',
    'form_field_id',
    'form_field_label',
    'form_response_phase',
)
FORM_RESPONSE_PHASES = {'primary', 'probe', 'revision'}


class ResponseWriteValidationError(ValueError):
    def __init__(self, message: str, code: str, index: int | None = None):
        super().__init__(message)
        self.code = code
        self.index = index


def _invalid(message: str, code: str, index: int) -> ResponseWriteValidationError:
    return ResponseWriteValidationError(message, code, index)


def _normalize_payload(payload: Any, index: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _invalid('message must be an object', 'invalid_message', index)

    required = ('session_id', 'sent_by', 'content', 'gpt_id')
    missing = [
        field
        for field in required
        if payload.get(field) is None
        or (isinstance(payload.get(field), str) and not payload.get(field).strip())
    ]
    if missing:
        raise _invalid(
            'missing fields: ' + ', '.join(missing),
            'missing_fields',
            index,
        )

    session_id = payload['session_id']
    sent_by = payload['sent_by']
    content = payload['content']
    student_id = payload.get('student_id', '')
    gpt_used = payload.get('gpt_used') or ''
    if not all(isinstance(value, str) for value in (
        session_id,
        sent_by,
        content,
        student_id,
        gpt_used,
    )):
        raise _invalid(
            'session_id, student_id, sent_by, content, and gpt_used must be strings',
            'invalid_fields',
            index,
        )

    session_id = session_id.strip()
    limits = {
        'session_id': FeedbackMessage._meta.get_field('session_id').max_length,
        'student_id': FeedbackMessage._meta.get_field('student_id').max_length,
        'sent_by': FeedbackMessage._meta.get_field('sent_by').max_length,
        'gpt_used': FeedbackMessage._meta.get_field('gpt_used').max_length,
    }
    values = {
        'session_id': session_id,
        'student_id': student_id,
        'sent_by': sent_by,
        'gpt_used': gpt_used,
    }
    overlong = [name for name, value in values.items() if len(value) > limits[name]]
    if overlong:
        raise _invalid(
            'fields exceed maximum length: ' + ', '.join(overlong),
            'invalid_fields',
            index,
        )

    raw_gpt_id = payload['gpt_id']
    if isinstance(raw_gpt_id, bool):
        raise _invalid('gpt_id must identify a survey', 'invalid_survey', index)
    try:
        gpt_id = int(raw_gpt_id)
    except (TypeError, ValueError):
        raise _invalid('gpt_id must identify a survey', 'invalid_survey', index)
    if gpt_id < 1:
        raise _invalid('gpt_id must identify a survey', 'invalid_survey', index)

    normalized = {
        'session_id': session_id,
        'student_id': student_id,
        'sent_by': sent_by,
        'content': content,
        'gpt_used': gpt_used,
        'gpt_id': gpt_id,
        'research_consent': bool(payload.get('research_consent', False)),
        'referred': bool(payload.get('referred', False)),
        'source': FeedbackMessage.SOURCE_CHAT,
    }
    for field in FORM_ATTRIBUTION_FIELDS:
        value = payload.get(field)
        if field == 'form_response_phase':
            normalized[field] = value if value in FORM_RESPONSE_PHASES else None
            continue
        if value is None:
            normalized[field] = None
            continue
        value = str(value).strip()
        normalized[field] = value or None
    return normalized


def persist_feedback_messages(payloads: list[dict]) -> list[FeedbackMessage]:
    if not isinstance(payloads, list):
        raise ResponseWriteValidationError(
            message='messages must be a list',
            code='invalid_messages',
        )
    if not payloads:
        raise ResponseWriteValidationError(
            message='messages list is empty',
            code='empty_messages',
        )

    normalized = [
        _normalize_payload(payload, index)
        for index, payload in enumerate(payloads)
    ]
    survey_ids = {message['gpt_id'] for message in normalized}

    with transaction.atomic():
        surveys = {
            survey.pk: survey
            for survey in FeedbackGPT.objects.select_related('course').filter(pk__in=survey_ids)
        }
        for index, message in enumerate(normalized):
            survey = surveys.get(message['gpt_id'])
            if survey is None:
                raise _invalid('survey does not exist', 'invalid_survey', index)

        grouped_indexes: dict[tuple[int, str], list[int]] = defaultdict(list)
        for index, message in enumerate(normalized):
            if surveys[message['gpt_id']].course_id is not None:
                grouped_indexes[(message['gpt_id'], message['session_id'])].append(index)

        sessions: dict[tuple[int, str], ResponseSession] = {}
        for survey_id, client_session_id in sorted(grouped_indexes):
            survey = surveys[survey_id]
            session, _ = ResponseSession.objects.get_or_create(
                survey=survey,
                client_session_id=client_session_id,
                defaults={
                    'course': survey.course,
                    'source': ResponseSession.SOURCE_STUDENT,
                },
            )
            session = ResponseSession.objects.select_for_update().get(pk=session.pk)
            if session.course_id != survey.course_id:
                raise ResponseWriteValidationError(
                    message='response session course does not match survey course',
                    code='session_course_mismatch',
                )
            sessions[(survey_id, client_session_id)] = session

        for key in sorted(grouped_indexes):
            session = sessions[key]
            indexes = grouped_indexes[key]
            next_sequence = session.next_message_sequence
            for offset, index in enumerate(indexes):
                normalized[index]['response_session'] = session
                normalized[index]['sequence'] = next_sequence + offset
            session.next_message_sequence = next_sequence + len(indexes)
            session.save(update_fields=['next_message_sequence'])

        messages = [FeedbackMessage(**message) for message in normalized]
        FeedbackMessage.objects.bulk_create(messages)
        return messages
