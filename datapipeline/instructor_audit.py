import re
import uuid

from django.core.exceptions import ValidationError

from datapipeline.models import FeedbackGPT, InstructorAuditEvent, LEAIChatSession


MAX_CHANGED_FIELDS = 100
MAX_METADATA_INTEGER = (2 ** 63) - 1
INSTITUTION_SLUG_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{0,99}$')

ALLOWED_METADATA_KEYS = {
    InstructorAuditEvent.ACTION_LOGIN_SUCCEEDED: frozenset(),
    InstructorAuditEvent.ACTION_LOGIN_DENIED: frozenset(),
    InstructorAuditEvent.ACTION_LOGOUT: frozenset(),
    InstructorAuditEvent.ACTION_PASSWORD_CHANGED: frozenset(),
    InstructorAuditEvent.ACTION_PROFILE_UPDATED: frozenset({'changed_fields'}),
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

ALLOWED_CHANGED_FIELDS = {
    InstructorAuditEvent.ACTION_PROFILE_UPDATED: frozenset({
        'display_name',
        'email',
    }),
    InstructorAuditEvent.ACTION_COURSE_BANNER_UPDATED: frozenset({
        'banner_enabled',
        'banner_text',
        'banner_dismissible',
        'banner_display_mode',
        'banner_duration_seconds',
        'banner_split_enabled',
        'banner_split_mode',
        'banner_split_value',
    }),
    InstructorAuditEvent.ACTION_COURSE_CUSTOMIZATION_UPDATED: frozenset({
        'bot_display_name',
        'referral_enabled',
        'referral_text',
        'identity_tracking_enabled',
        'completion_certificate_enabled',
        'parsed_document_download_enabled',
    }),
    InstructorAuditEvent.ACTION_SURVEY_UPDATED: frozenset({
        'name',
        'survey_label',
        'week_number',
        'instructions',
        'anonymity_mode',
        'reporting_structure',
        'expires_at',
        'opens_at',
        'team_configuration_id',
        'form_schema_id',
    }),
    InstructorAuditEvent.ACTION_ANALYSIS_SESSION_UPDATED: frozenset({
        'title',
        'system_prompt_override',
        'scope_kind',
        'scope_week_number',
        'scope_survey_ids',
        'scope_session_ids',
    }),
    InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_UPDATED: frozenset({
        'name',
        'label_prefix',
        'color',
        'archived',
        'teams',
    }),
}

SURVEY_MODES = frozenset(value for value, _label in FeedbackGPT.MODE_CHOICES)
ANALYSIS_SCOPE_KINDS = frozenset(
    value for value, _label in LEAIChatSession.SCOPE_CHOICES
)
SURVEY_STATUSES = frozenset({'open', 'closed'})
AUTHORIZATION_REASON_CODES = frozenset({
    'institution_access_denied',
    'course_access_denied',
    'capability_denied',
})

TARGET_TYPE_BY_ACTION = {
    InstructorAuditEvent.ACTION_LOGIN_SUCCEEDED: None,
    InstructorAuditEvent.ACTION_LOGIN_DENIED: None,
    InstructorAuditEvent.ACTION_LOGOUT: None,
    InstructorAuditEvent.ACTION_PASSWORD_CHANGED: None,
    InstructorAuditEvent.ACTION_PROFILE_UPDATED: None,
    InstructorAuditEvent.ACTION_COURSE_CREATED: 'course',
    InstructorAuditEvent.ACTION_COURSE_BANNER_UPDATED: 'course',
    InstructorAuditEvent.ACTION_COURSE_CUSTOMIZATION_UPDATED: 'course',
    InstructorAuditEvent.ACTION_SURVEY_CREATED: 'survey',
    InstructorAuditEvent.ACTION_SURVEY_UPDATED: 'survey',
    InstructorAuditEvent.ACTION_SURVEY_STATUS_CHANGED: 'survey',
    InstructorAuditEvent.ACTION_SURVEY_CLONED: 'survey',
    InstructorAuditEvent.ACTION_SURVEY_DELETED: 'survey',
    InstructorAuditEvent.ACTION_SURVEY_RESPONSES_EXPORTED: 'survey',
    InstructorAuditEvent.ACTION_ANALYSIS_SESSION_CREATED: 'analysis_session',
    InstructorAuditEvent.ACTION_ANALYSIS_SESSION_UPDATED: 'analysis_session',
    InstructorAuditEvent.ACTION_ANALYSIS_SESSION_DELETED: 'analysis_session',
    InstructorAuditEvent.ACTION_ANALYSIS_TURN_STARTED: 'analysis_session',
    InstructorAuditEvent.ACTION_ANALYSIS_QUICKTAKE_GENERATED: 'quicktake',
    InstructorAuditEvent.ACTION_ANALYSIS_QUICKTAKE_DELETED: 'quicktake',
    InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_CREATED: 'team_configuration',
    InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_UPDATED: 'team_configuration',
    InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_ARCHIVED: 'team_configuration',
    InstructorAuditEvent.ACTION_TEAM_CONFIGURATION_DELETED: 'team_configuration',
    InstructorAuditEvent.ACTION_PDF_INGEST_STARTED: 'pdf_ingest_job',
    InstructorAuditEvent.ACTION_PDF_INGEST_ABANDONED: 'pdf_ingest_job',
    InstructorAuditEvent.ACTION_PDF_INGEST_COMMITTED: 'pdf_ingest_batch',
    InstructorAuditEvent.ACTION_PDF_INGEST_REVERTED: 'pdf_ingest_batch',
    InstructorAuditEvent.ACTION_AUTHORIZATION_DENIED: 'course',
}

INTEGER_TARGET_TYPES = frozenset({
    'survey',
    'quicktake',
    'team_configuration',
})
UUID_TARGET_TYPES = frozenset({
    'analysis_session',
    'pdf_ingest_job',
    'pdf_ingest_batch',
})


def _validate_fixed_string(value, allowed_values):
    if type(value) is not str or value not in allowed_values:
        raise ValidationError({'metadata': 'Metadata value is not allowed.'})


def _validate_institution_slug(value):
    if (
        type(value) is not str
        or INSTITUTION_SLUG_PATTERN.fullmatch(value) is None
    ):
        raise ValidationError({'metadata': 'institution_slug is not valid.'})


def _validate_positive_id(value):
    if type(value) is not int or not 1 <= value <= MAX_METADATA_INTEGER:
        raise ValidationError({'metadata': 'Metadata ID must be a positive integer.'})


def _validate_count(value):
    if type(value) is not int or not 0 <= value <= MAX_METADATA_INTEGER:
        raise ValidationError({'metadata': 'Metadata count must be a non-negative integer.'})


METADATA_VALUE_VALIDATORS = {
    'institution_slug': _validate_institution_slug,
    'mode': lambda value: _validate_fixed_string(value, SURVEY_MODES),
    'status': lambda value: _validate_fixed_string(value, SURVEY_STATUSES),
    'source_survey_id': _validate_positive_id,
    'responses_deleted': _validate_count,
    'row_count': _validate_count,
    'scope_kind': lambda value: _validate_fixed_string(
        value,
        ANALYSIS_SCOPE_KINDS,
    ),
    'file_count': _validate_count,
    'student_count': _validate_count,
    'message_count': _validate_count,
    'deleted_count': _validate_count,
    'reason_code': lambda value: _validate_fixed_string(
        value,
        AUTHORIZATION_REASON_CODES,
    ),
}


def _validate_changed_fields(action, value):
    if not isinstance(value, list) or len(value) > MAX_CHANGED_FIELDS:
        raise ValidationError({'metadata': 'changed_fields must be a bounded list.'})

    allowed_fields = ALLOWED_CHANGED_FIELDS[action]
    if (
        any(type(field_name) is not str for field_name in value)
        or len(value) != len(set(value))
        or not set(value).issubset(allowed_fields)
    ):
        raise ValidationError({
            'metadata': 'changed_fields contains a field not allowed for this action.',
        })


def _validated_target(*, action, target_type, target_id, course):
    if type(target_type) is not str:
        raise ValidationError({'target_type': 'Target type is not allowed.'})
    if target_type == '':
        if target_id != '':
            raise ValidationError({'target_id': 'Target ID requires a target type.'})
        return '', ''

    expected_type = TARGET_TYPE_BY_ACTION[action]
    if target_type != expected_type:
        raise ValidationError({'target_type': 'Target type is not allowed for this action.'})

    if target_type == 'course':
        if (
            type(target_id) is not str
            or course is None
            or target_id != course.course_id
        ):
            raise ValidationError({'target_id': 'Course target must match the event course.'})
        return target_type, target_id

    if target_type in INTEGER_TARGET_TYPES:
        if type(target_id) is not int or not 1 <= target_id <= MAX_METADATA_INTEGER:
            raise ValidationError({'target_id': 'Target ID must be a positive integer.'})
        return target_type, str(target_id)

    if target_type in UUID_TARGET_TYPES:
        if type(target_id) is not uuid.UUID:
            raise ValidationError({'target_id': 'Target ID must be a UUID.'})
        return target_type, str(target_id)

    raise ValidationError({'target_type': 'Target type is not allowed.'})


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
            _validate_changed_fields(action, value)
        else:
            validator = METADATA_VALUE_VALIDATORS.get(key)
            if validator is None:
                raise ValidationError({'metadata': 'Metadata key has no validator.'})
            validator(value)

    if 'institution_slug' in metadata and (
        course is None
        or course.institution_id is None
        or metadata['institution_slug'] != course.institution.slug
    ):
        raise ValidationError({
            'metadata': 'institution_slug must match the event course institution.',
        })

    target_type, target_id = _validated_target(
        action=action,
        target_type=target_type,
        target_id=target_id,
        course=course,
    )

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
