from __future__ import annotations

import copy
import json
import uuid

from django.db import transaction
from django.utils import timezone

from . import openai_client
from .models import (
    AuthoringConversation,
    AuthoringMessage,
    AuthoringRun,
    AuthoringRunSource,
    QuestionSetDraft,
)
from .question_sets import (
    QuestionSetError,
    save_feedback_draft,
    validate_feedback_body,
)


AUTHORING_RESPONSE_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['operations', 'summary', 'rationale', 'assistant_message'],
    'properties': {
        'operations': {
            'type': 'array',
            'maxItems': 24,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': [
                    'op', 'section_id', 'question_id', 'value', 'title',
                    'short_label', 'prompt', 'follow_up_enabled',
                    'follow_up_prompt', 'to_index',
                ],
                'properties': {
                    'op': {
                        'type': 'string',
                        'enum': [
                            'set_title', 'set_intro', 'set_closing',
                            'set_opening_prompt', 'set_listening_goal',
                            'add_section', 'rename_section', 'remove_section',
                            'move_section', 'add_question', 'update_question',
                            'remove_question', 'move_question',
                        ],
                    },
                    'section_id': {'type': ['string', 'null']},
                    'question_id': {'type': ['string', 'null']},
                    'value': {'type': ['string', 'null']},
                    'title': {'type': ['string', 'null']},
                    'short_label': {'type': ['string', 'null']},
                    'prompt': {'type': ['string', 'null']},
                    'follow_up_enabled': {'type': ['boolean', 'null']},
                    'follow_up_prompt': {'type': ['string', 'null']},
                    'to_index': {'type': ['integer', 'null']},
                },
            },
        },
        'summary': {'type': 'string', 'maxLength': 240},
        'rationale': {'type': 'string', 'maxLength': 2000},
        'assistant_message': {'type': 'string', 'maxLength': 2000},
    },
}


def _require_operation(operation, name):
    value = operation.get(name)
    if value is None:
        raise QuestionSetError('invalid_authoring_operation', f'{name} is required.')
    return value


def _find_section(body, section_id):
    return next((item for item in body['sections'] if item['id'] == section_id), None)


def _find_question(body, question_id):
    for section in body['sections']:
        for question in section['questions']:
            if question['id'] == question_id:
                return section, question
    return None, None


def _move(items, current_index, to_index):
    if type(to_index) is not int or not 0 <= to_index < len(items):
        raise QuestionSetError('invalid_authoring_operation', 'Move destination is invalid.')
    item = items.pop(current_index)
    items.insert(to_index, item)


def apply_feedback_operations(body, operations, *, audience, collection_style):
    if not isinstance(operations, list) or len(operations) > 24:
        raise QuestionSetError('invalid_authoring_operation')
    result = copy.deepcopy(body)
    for operation in operations:
        if not isinstance(operation, dict):
            raise QuestionSetError('invalid_authoring_operation')
        op = operation.get('op')
        if op == 'set_title':
            result['title'] = _require_operation(operation, 'value')
        elif collection_style == 'open':
            field_by_operation = {
                'set_opening_prompt': 'opening_prompt',
                'set_listening_goal': 'listening_goal',
                'set_closing': 'closing_prompt',
            }
            field_name = field_by_operation.get(op)
            if field_name is None:
                raise QuestionSetError('invalid_authoring_operation')
            result[field_name] = _require_operation(operation, 'value')
        elif op == 'set_intro':
            result['intro'] = _require_operation(operation, 'value')
        elif op == 'set_closing':
            result['closing']['prompt'] = _require_operation(operation, 'value')
        elif op == 'add_section':
            result['sections'].append({
                'id': str(uuid.uuid4()),
                'title': _require_operation(operation, 'title'),
                'questions': [{
                    'id': str(uuid.uuid4()),
                    'short_label': operation.get('short_label') or 'New question',
                    'prompt': operation.get('prompt') or 'What would you like to share?',
                    'follow_up': {
                        'enabled': bool(operation.get('follow_up_enabled')),
                        'prompt': operation.get('follow_up_prompt') or '',
                    },
                    'response_kind': 'long_text',
                }],
            })
        elif op in {'rename_section', 'remove_section', 'move_section', 'add_question'}:
            section_id = _require_operation(operation, 'section_id')
            section = _find_section(result, section_id)
            if section is None:
                raise QuestionSetError('invalid_authoring_operation', 'Section was not found.')
            if op == 'rename_section':
                section['title'] = _require_operation(operation, 'title')
            elif op == 'remove_section':
                result['sections'].remove(section)
            elif op == 'move_section':
                _move(
                    result['sections'],
                    result['sections'].index(section),
                    operation.get('to_index'),
                )
            else:
                section['questions'].append({
                    'id': str(uuid.uuid4()),
                    'short_label': _require_operation(operation, 'short_label'),
                    'prompt': _require_operation(operation, 'prompt'),
                    'follow_up': {
                        'enabled': bool(operation.get('follow_up_enabled')),
                        'prompt': operation.get('follow_up_prompt') or '',
                    },
                    'response_kind': 'long_text',
                })
        elif op in {'update_question', 'remove_question', 'move_question'}:
            question_id = _require_operation(operation, 'question_id')
            section, question = _find_question(result, question_id)
            if question is None:
                raise QuestionSetError('invalid_authoring_operation', 'Question was not found.')
            if op == 'remove_question':
                section['questions'].remove(question)
                if not section['questions']:
                    result['sections'].remove(section)
            elif op == 'move_question':
                destination_id = operation.get('section_id') or section['id']
                destination = _find_section(result, destination_id)
                if destination is None:
                    raise QuestionSetError('invalid_authoring_operation', 'Destination section was not found.')
                section['questions'].remove(question)
                if not section['questions']:
                    result['sections'].remove(section)
                destination['questions'].insert(operation.get('to_index'), question)
            else:
                for source, target in (
                    ('short_label', 'short_label'),
                    ('prompt', 'prompt'),
                ):
                    if operation.get(source) is not None:
                        question[target] = operation[source]
                if operation.get('follow_up_enabled') is not None:
                    question['follow_up']['enabled'] = operation['follow_up_enabled']
                if operation.get('follow_up_prompt') is not None:
                    question['follow_up']['prompt'] = operation['follow_up_prompt']
        else:
            raise QuestionSetError('invalid_authoring_operation')
    normalized, _validation = validate_feedback_body(
        result,
        audience=audience,
        collection_style=collection_style,
    )
    return normalized


def _conversation_messages(conversation):
    role_map = {'instructor': 'user', 'assistant': 'assistant', 'system': 'system'}
    return [
        {'role': role_map[message.role], 'content': message.content}
        for message in conversation.messages.order_by('sequence', 'id')
    ]


def _next_message(conversation, *, role, content, author=None, resulting_version=None):
    message = AuthoringMessage.objects.create(
        conversation=conversation,
        sequence=conversation.next_message_sequence,
        role=role,
        content=content,
        author=author,
        resulting_version=resulting_version,
    )
    conversation.next_message_sequence += 1
    conversation.save(update_fields=['next_message_sequence', 'updated_at'])
    return message


def _provider_prompt(draft, instruction):
    course = draft.question_set.course
    return json.dumps({
        'course': {
            'code': course.course_id,
            'title': course.course_name,
        },
        'feedback_type': {
            'audience': draft.question_set.audience,
            'collection_style': draft.question_set.collection_style,
        },
        'current_design': draft.body,
        'instructor_request': instruction,
    }, ensure_ascii=False)


def run_authoring_request(
    *,
    draft_id,
    actor,
    instructor_session,
    instruction,
    expected_version,
    idempotency_key,
):
    if not isinstance(instruction, str) or not instruction.strip():
        raise QuestionSetError('invalid_authoring_request')
    instruction = instruction.strip()[:4000]
    if not isinstance(idempotency_key, str) or len(idempotency_key.strip()) < 8:
        raise QuestionSetError('invalid_idempotency_key')
    idempotency_key = idempotency_key.strip()[:100]

    with transaction.atomic():
        draft = (
            QuestionSetDraft.objects
            .select_for_update(of=('self',))
            .select_related('question_set__course', 'current_checkpoint')
            .get(public_id=draft_id)
        )
        if draft.version != expected_version:
            raise QuestionSetError('stale_draft')
        conversation, _created = AuthoringConversation.objects.get_or_create(
            question_set=draft.question_set,
            defaults={
                'course': draft.question_set.course,
                'created_by': actor,
            },
        )
        existing = AuthoringRun.objects.filter(
            conversation=conversation,
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            if existing.request_message.content != instruction:
                raise QuestionSetError('idempotency_key_conflict')
            return existing
        request_message = _next_message(
            conversation,
            role='instructor',
            content=instruction,
            author=actor,
        )
        run = AuthoringRun.objects.create(
            conversation=conversation,
            request_message=request_message,
            base_version=draft.current_checkpoint,
            status='running',
            idempotency_key=idempotency_key,
            started_at=timezone.now(),
        )
        AuthoringRunSource.objects.create(
            run=run,
            source_type='authoring_rule',
            stable_identifier='leai-feedback-authoring-v12-1',
            content_hash='rules-v12-1',
            label='LEAI feedback authoring rules',
            rank=1,
        )
        chat_history = _conversation_messages(conversation)[:-1]
        user_text = _provider_prompt(draft, instruction)

    try:
        provider_result = openai_client.run_structured(
            chat_history=[{
                'role': 'system',
                'content': (
                    'Act as an instructional feedback-design partner. Return only '
                    'the smallest valid operations needed. Never invent course facts.'
                ),
            }, *chat_history],
            user_text=user_text,
            json_schema=AUTHORING_RESPONSE_SCHEMA,
            schema_name='leai_feedback_authoring_v12',
        )
        parsed = provider_result['parsed']
        operations = parsed.get('operations')
        changed_body = apply_feedback_operations(
            draft.body,
            operations,
            audience=draft.question_set.audience,
            collection_style=draft.question_set.collection_style,
        )
    except QuestionSetError:
        AuthoringRun.objects.filter(pk=run.pk).update(
            status='failed',
            error_code='invalid_operations',
            completed_at=timezone.now(),
        )
        raise
    except openai_client.OpenAIClientError:
        AuthoringRun.objects.filter(pk=run.pk).update(
            status='failed',
            error_code='provider_error',
            completed_at=timezone.now(),
        )
        raise QuestionSetError('authoring_provider_error')

    with transaction.atomic():
        current = (
            QuestionSetDraft.objects
            .select_for_update(of=('self',))
            .select_related('question_set__course', 'current_checkpoint')
            .get(pk=draft.pk)
        )
        if current.version != expected_version or current.current_checkpoint_id != run.base_version_id:
            run.status = 'conflict'
            run.validated_operations = operations
            run.completed_at = timezone.now()
            run.save(update_fields=['status', 'validated_operations', 'completed_at'])
            raise QuestionSetError('authoring_conflict')
        saved = save_feedback_draft(
            draft_id=current.public_id,
            actor=actor,
            instructor_session=instructor_session,
            expected_version=current.version,
            body=changed_body,
            idempotency_key=f'ai:{idempotency_key}',
            checkpoint_reason='ai',
            author_kind='ai',
            summary=parsed.get('summary') or 'AI changes',
            rationale=parsed.get('rationale') or '',
            change_set=operations,
        )
        saved.refresh_from_db()
        run.status = 'applied'
        run.model = provider_result.get('model') or ''
        run.requested_operations = operations
        run.validated_operations = operations
        run.change_summary = (parsed.get('summary') or '')[:240]
        run.rationale = parsed.get('rationale') or ''
        run.applied_version = saved.current_checkpoint
        run.completed_at = timezone.now()
        run.save(update_fields=[
            'status', 'model', 'requested_operations', 'validated_operations',
            'change_summary', 'rationale', 'applied_version', 'completed_at',
        ])
        _next_message(
            conversation,
            role='assistant',
            content=parsed.get('assistant_message') or run.change_summary,
            resulting_version=saved.current_checkpoint,
        )
        return run


def serialize_authoring_conversation(conversation):
    return {
        'id': str(conversation.public_id),
        'status': conversation.status,
        'messages': [{
            'sequence': message.sequence,
            'role': message.role,
            'content': message.content,
            'resulting_version_id': (
                str(message.resulting_version.public_id)
                if message.resulting_version_id else None
            ),
            'created_at': message.created_at.isoformat(),
        } for message in conversation.messages.select_related(
            'resulting_version',
        ).order_by('sequence', 'id')],
    }


def serialize_authoring_run(run):
    return {
        'id': str(run.public_id),
        'status': run.status,
        'model': run.model,
        'change_summary': run.change_summary,
        'rationale': run.rationale,
        'applied_version_id': (
            str(run.applied_version.public_id) if run.applied_version_id else None
        ),
        'error_code': run.error_code,
    }
