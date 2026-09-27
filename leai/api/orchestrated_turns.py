"""Atomic Option 1 adapter using the existing student capability and storage."""
import hashlib
import json
import time
import uuid
from contextlib import contextmanager

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from datapipeline import orchestration_client
from leai.models import AuditEvent, ResponseMessage, ResponseSession, SurveyOccurrence
from leai.models.responses import MutationReceipt
from leai.services.orchestration import items
from .environment import no_store_json


def enabled(protocol):
    # Local evaluation gate; no hosted traffic changes until acceptance/deployment.
    return (getattr(settings, 'LEAI_ORCHESTRATION_ENABLED', False)
            and all(i['response']['kind'] == 'text' for i in items(protocol)))


def _audit(session, attempt_id, action, outcome, metadata):
    AuditEvent.objects.create(actor_kind='system', course=session.occurrence.course,
        action=action, outcome=outcome, target_type='response_session',
        target_id=str(session.public_id), request_id=attempt_id, bounded_metadata=metadata)


def _record_usage(session, attempt_id, metrics):
    # One bounded event per provider request. No transcript, proposal or tool arguments.
    keys = ('attempt', 'client_request_id', 'request_id', 'response_id', 'model', 'status',
            'service_tier', 'input_tokens', 'cached_input_tokens', 'cache_write_tokens',
            'output_tokens', 'reasoning_tokens', 'total_tokens', 'duration_ms', 'ttft_ms',
            'error_type', 'http_status', 'incomplete_reason')
    scalar = lambda value: value[:128] if isinstance(value, str) else value if type(value) in (int, float, bool) else None
    for call in metrics.get('calls') or [{}]:
        _audit(session, attempt_id, 'student.orchestration_usage',
            'allowed' if metrics.get('outcome') == 'validated' else 'failed',
            {'expected_version': session.turn_version, 'model_outcome': scalar(metrics.get('outcome')),
             'arm': scalar(metrics.get('arm')), 'prompt_version': scalar(metrics.get('prompt_version')),
             'protocol_sha256': scalar(metrics.get('protocol_sha256')),
             'turn_processing_ms': scalar(metrics.get('turn_processing_ms')),
             'call': {key: scalar(call.get(key)) for key in keys}})


@contextmanager
def _record_disposition(session, attempt_id):
    disposition = {'disposition': 'commit_error'}
    try:
        yield disposition
    except Exception:
        disposition['disposition'] = 'commit_error'
        raise
    finally:
        # This context wraps transaction.atomic, so even rollback preserves the outcome.
        _audit(session, attempt_id, 'student.orchestration_disposition',
            'allowed' if disposition['disposition'] == 'committed' else 'failed', disposition)


def process(request, occurrence, session, student, expected_version, request_id):
    from .student_responses import _available, _messages, _session_payload
    started = time.perf_counter()
    request_hash = hashlib.sha256(json.dumps({'version': expected_version, 'student': student}, sort_keys=True).encode()).hexdigest()
    receipt_scope = {
        'principal_scope': f'student:{session.pk}', 'operation': 'orchestrated_turn',
        'target_key': str(session.pk),
        'idempotency_key_hash': hashlib.sha256(request_id.encode()).hexdigest(),
    }
    receipt = MutationReceipt.objects.filter(**receipt_scope).first()
    if receipt:
        if receipt.request_hash != request_hash:
            return no_store_json({'error': 'idempotency_conflict'}, status=409)
        session.refresh_from_db()
        return no_store_json(_session_payload(session))
    if session.status != 'active' or session.turn_version != expected_version:
        return no_store_json({'error': 'stale_turn'}, status=409)
    if not _available(occurrence):
        return no_store_json({'error': 'survey_closed'}, status=403)
    content = student.get('text', '')
    if student['kind'] == 'skip':
        content = 'I prefer not to answer this question.'
    elif student['kind'] != 'text':
        return no_store_json({'error': 'invalid_turn'}, status=422)
    messages = _messages(session)
    messages.append({'sequence': session.next_message_sequence, 'role': 'student', 'content': content})
    attempt_id = str(uuid.uuid4())
    _audit(session, attempt_id, 'student.orchestration_attempt', 'allowed',
           {'expected_version': expected_version})
    try:
        result = orchestration_client.run_orchestration(occurrence.revision.compiled_protocol, session.flow_state, messages,
                                  tools_enabled=getattr(settings, 'LEAI_ORCHESTRATION_TOOLS', False))
    except orchestration_client.OrchestrationUnavailable as error:
        _record_usage(session, attempt_id, error.metrics)
        return no_store_json({'error': 'assessment_unavailable', 'retryable': True}, status=503)
    _record_usage(session, attempt_id, result['metrics'])
    with _record_disposition(session, attempt_id) as disposition, transaction.atomic():
        locked = ResponseSession.objects.select_for_update().get(pk=session.pk)
        receipt = MutationReceipt.objects.filter(**receipt_scope).first()
        if receipt:
            if receipt.request_hash != request_hash:
                disposition['disposition'] = 'idempotency_conflict'
                return no_store_json({'error': 'idempotency_conflict'}, status=409)
            disposition['disposition'] = 'duplicate_discarded'
            return no_store_json(_session_payload(locked))
        if locked.status != 'active' or locked.turn_version != expected_version:
            disposition['disposition'] = 'stale_turn'
            return no_store_json({'error': 'stale_turn'}, status=409)
        fresh_occurrence = SurveyOccurrence.objects.select_related('revision__question_set', 'course').get(pk=occurrence.pk)
        if not _available(fresh_occurrence):
            disposition['disposition'] = 'survey_closed'
            return no_store_json({'error': 'survey_closed'}, status=403)
        sequence = locked.next_message_sequence
        state = result['state']
        updates = result['proposal']['updates']
        active_items = items(occurrence.revision.compiled_protocol)
        old_index = locked.flow_state['item_index']
        old_id = active_items[old_index]['id'] if old_index < len(active_items) else None
        new_index = state['item_index']
        new_id = active_items[new_index]['id'] if new_index < len(active_items) else None
        ResponseMessage.objects.bulk_create([
            ResponseMessage(response_session=locked, sequence=sequence, role='student', content=content,
                attribution={'item_id': old_id, 'phase': locked.flow_state['phase'], 'kind': 'text',
                             'evidence_for': [u['item_id'] for u in updates], 'covered_targets': []}),
            ResponseMessage(response_session=locked, sequence=sequence + 1, role='assistant', content=result['reply'],
                attribution={'item_id': new_id, 'phase': state['phase'],
                             'orchestration_metrics': result['metrics']}),
        ])
        # Operational metadata is available only to the existing authorized debug endpoint.
        state['last_turn_diagnostics'] = result['metrics']
        locked.flow_state = state
        locked.turn_version += 1
        locked.next_message_sequence += 2
        locked.save(update_fields=['flow_state', 'turn_version', 'next_message_sequence', 'updated_at'])
        MutationReceipt.objects.create(**receipt_scope, request_hash=request_hash,
            result={'turn_version': locked.turn_version, 'assistant_sequence': sequence + 1}, completed_at=timezone.now())
        disposition['disposition'] = 'committed'
    response = no_store_json(_session_payload(locked))
    response['Server-Timing'] = f'leai_turn;dur={(time.perf_counter() - started) * 1000:.3f}'
    return response
