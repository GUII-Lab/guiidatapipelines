import math
import re

from django.core.exceptions import ValidationError

from datapipeline.models import InstructorAuditEvent


MAX_METADATA_STRING_LENGTH = 100
MAX_CHANGED_FIELDS = 100
MAX_METADATA_INTEGER = (2 ** 63) - 1
FIELD_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9_]*$')

ALLOWED_METADATA_KEYS = {
    InstructorAuditEvent.ACTION_LOGIN_SUCCEEDED: frozenset(),
    InstructorAuditEvent.ACTION_LOGIN_DENIED: frozenset(),
    InstructorAuditEvent.ACTION_LOGOUT: frozenset(),
    InstructorAuditEvent.ACTION_PASSWORD_CHANGED: frozenset(),
    InstructorAuditEvent.ACTION_PROFILE_UPDATED: frozenset({'changed_field'}),
    InstructorAuditEvent.ACTION_COURSE_CREATED: frozenset({'institution_slug'}),
    InstructorAuditEvent.ACTION_COURSE_BANNER_UPDATED: frozenset({'changed_fields'}),
    InstructorAuditEvent.ACTION_COURSE_CUSTOMIZATION_UPDATED: frozenset({'changed_fields'}),
    InstructorAuditEvent.ACTION_SURVEY_CREATED: frozenset({'mode'}),
    InstructorAuditEvent.ACTION_SURVEY_UPDATED: frozenset({'changed_fields'}),
    InstructorAuditEvent.ACTION_SURVEY_STATUS_CHANGED: frozenset({'status'}),
    InstructorAuditEvent.ACTION_SURVEY_CLONED: frozenset({'source_survey_id'}),
    InstructorAuditEvent.ACTION_SURVEY_DELETED: frozenset({'responses_deleted'}),
    InstructorAuditEvent.ACTION_SURVEY_RESPONSES_EXPORTED: frozenset({'row_count'}),
    InstructorAuditEvent.ACTION_ANALYSIS_SESSION_CREATED: frozenset(),
    InstructorAuditEvent.ACTION_ANALYSIS_SESSION_UPDATED: frozenset({'changed_fields'}),
    InstructorAuditEvent.ACTION_ANALYSIS_SESSION_DELETED: frozenset(),
    InstructorAuditEvent.ACTION_ANALYSIS_TURN_STARTED: frozenset(),
    InstructorAuditEvent.ACTION_ANALYSIS_QUICKTAKE_GENERATED: frozenset({'scope_kind'}),
    InstructorAuditEvent.ACTION_ANALYSIS_QUICKTAKE_DELETED: frozenset(),
    InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_CREATED: frozenset(),
    InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_UPDATED: frozenset({'changed_fields'}),
    InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_ARCHIVED: frozenset(),
    InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_DELETED: frozenset(),
    InstructorAuditEvent.ACTION_PDF_INGEST_STARTED: frozenset({'file_count'}),
    InstructorAuditEvent.ACTION_PDF_INGEST_ABANDONED: frozenset(),
    InstructorAuditEvent.ACTION_PDF_INGEST_COMMITTED: frozenset({
        'student_count',
        'message_count',
    }),
    InstructorAuditEvent.ACTION_PDF_INGEST_REVERTED: frozenset({'deleted_count'}),
    InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED: frozenset({'reason_code'}),
}


def _validate_changed_fields(value):
    if not isinstance(value, list) or len(value) > MAX_CHANGED_FIELDS:
        raise ValidationError({'metadata': 'changed_fields must be a bounded list.'})

    for field_name in value:
        if (
            not isinstance(field_name, str)
            or len(field_name) > MAX_METADATA_STRING_LENGTH
            or not FIELD_NAME_PATTERN.fullmatch(field_name)
        ):
            raise ValidationError({
                'metadata': 'changed_fields must contain fixed field-name strings.',
            })


def _validate_scalar(value):
    if isinstance(value, str):
        if len(value) > MAX_METADATA_STRING_LENGTH:
            raise ValidationError({'metadata': 'Metadata strings may not exceed 100 characters.'})
        return

    if value is None or isinstance(value, bool):
        return

    if isinstance(value, int):
        if abs(value) > MAX_METADATA_INTEGER:
            raise ValidationError({'metadata': 'Metadata integers must fit in 64 bits.'})
        return

    if isinstance(value, float) and math.isfinite(value):
        return

    raise ValidationError({'metadata': 'Metadata values must be bounded scalars.'})


def record_instructor_event(
    *,
    action,
    outcome,
    actor=None,
    session=None,
    course=None,
    target_type='',
    target_id='',
    metadata=None,
) -> InstructorAuditEvent:
    valid_actions = {value for value, _label in InstructorAuditEvent.ACTION_CHOICES}
    valid_outcomes = {value for value, _label in InstructorAuditEvent.OUTCOME_CHOICES}
    if action not in valid_actions:
        raise ValidationError({'action': 'Select a valid choice.'})
    if outcome not in valid_outcomes:
        raise ValidationError({'outcome': 'Select a valid choice.'})

    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValidationError({'metadata': 'Metadata must be a dictionary.'})
    if not set(metadata).issubset(ALLOWED_METADATA_KEYS[action]):
        raise ValidationError({'metadata': 'Metadata contains keys not allowed for this action.'})

    for key, value in metadata.items():
        if key == 'changed_fields':
            _validate_changed_fields(value)
        else:
            _validate_scalar(value)

    event = InstructorAuditEvent(
        actor=actor,
        session=session,
        course=course,
        course_id_snapshot=course.course_id if course is not None else '',
        action=action,
        outcome=outcome,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata,
    )
    event.full_clean()
    event.save()
    return event
