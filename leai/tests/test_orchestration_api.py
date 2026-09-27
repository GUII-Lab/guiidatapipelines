import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from leai.models import ResponseSession, ResponseMessage, AuditEvent
from leai.models.responses import MutationReceipt
from leai.services.orchestration import preview_proposal
from leai.tests.test_orchestration import proposal, update
from leai.tests.test_response_models import ResponseFixturesMixin


@override_settings(LEAI_ORCHESTRATION_ENABLED=True)
class OrchestrationApiTests(ResponseFixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.protocol = json.loads((Path(__file__).parents[1] / 'fixtures/ulia_conversational.json').read_text())
        self.occurrence = self.make_occurrence(compiled_protocol=self.protocol)
        self.base = f'/datapipeline/api/v1/surveys/{self.occurrence.public_id}/'
        self.started = self.client.post(self.base + 'sessions/', data=json.dumps({
            'terms_consent': True, 'research_consent': False}), content_type='application/json').json()
        self.url = self.base + f"sessions/{self.started['session_id']}/turns/"
        self.auth = {'HTTP_AUTHORIZATION': f"Bearer {self.started['token']}"}

    def send(self, text='I use the task goal.', **extra):
        return self.client.post(self.url, data=json.dumps({
            'expected_version': 1, 'item_id': 'P1', 'kind': 'text', 'text': text,
            'request_id': 'smoke-request-001', **extra}), content_type='application/json', **self.auth)

    def fake_transport(self, protocol, state, messages, **kwargs):
        p = proposal([update()], action='ask_main', item='P2', reply='Thanks.')
        new, reply = preview_proposal(protocol, state, messages, p)
        return {'state': new, 'reply': reply, 'proposal': p,
                'metrics': {'outcome': 'validated', 'calls': [{'input_tokens': 100, 'output_tokens': 20}], 'turn_processing_ms': 5}}

    def test_retry_commits_once_and_changed_body_conflicts(self):
        with patch('datapipeline.orchestration_client.run_orchestration', side_effect=self.fake_transport):
            response = self.send()
            self.assertEqual(response.status_code, 200, response.content)
            again = self.send()
            self.assertEqual(again.status_code, 200)
            self.assertEqual(self.send('different text').status_code, 409)
        session = ResponseSession.objects.get(public_id=self.started['session_id'])
        self.assertEqual(session.turn_version, 2)
        self.assertEqual(session.messages.count(), 4)
        self.assertEqual(session.flow_state['answer_map']['P1'], [3])
        self.assertEqual(MutationReceipt.objects.count(), 1)
        self.assertEqual(response.json()['prompt']['item_id'], 'P2')
        self.assertEqual(response.json()['answer_excerpts']['P1'], ['I use the task goal.'])
        self.assertEqual(session.messages.get(sequence=4).attribution['orchestration_metrics']['calls'][0]['input_tokens'], 100)
        self.assertNotIn('metrics', response.content.decode())
        self.assertNotIn('last_turn_diagnostics', response.content.decode())
        self.assertNotIn('request_id', response.json()['messages'][-1]['attribution'])
        self.assertIn('Server-Timing', response)
        self.assertEqual(AuditEvent.objects.filter(action='student.orchestration_usage').count(), 1)
        self.assertEqual(AuditEvent.objects.get(action='student.orchestration_disposition').bounded_metadata['disposition'], 'committed')
        self.assertNotIn('I use the task goal', str(list(AuditEvent.objects.values_list('bounded_metadata', flat=True))))

    def test_provider_failure_does_not_write_student_or_advance(self):
        from datapipeline.orchestration_client import OrchestrationUnavailable
        with patch('datapipeline.orchestration_client.run_orchestration', side_effect=OrchestrationUnavailable({'calls': []})):
            response = self.send()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(ResponseMessage.objects.count(), 2)
        self.assertEqual(ResponseSession.objects.get(public_id=self.started['session_id']).turn_version, 1)
        self.assertTrue(AuditEvent.objects.filter(action='student.orchestration_usage', outcome='failed').exists())

    def test_late_proposal_cannot_overwrite_finalized_session(self):
        def finalized(*args, **kwargs):
            result = self.fake_transport(*args, **kwargs)
            ResponseSession.objects.filter(public_id=self.started['session_id']).update(status='completed', turn_version=2)
            return result
        with patch('datapipeline.orchestration_client.run_orchestration', side_effect=finalized):
            response = self.send()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ResponseMessage.objects.count(), 2)
        audit = AuditEvent.objects.get(action='student.orchestration_usage')
        self.assertEqual(audit.bounded_metadata['call']['input_tokens'], 100)
        self.assertEqual(AuditEvent.objects.get(action='student.orchestration_disposition').bounded_metadata['disposition'], 'stale_turn')

    def test_unrelated_session_capability_cannot_call_model(self):
        self.auth = {'HTTP_AUTHORIZATION': 'Bearer ' + 'f' * 64}
        response = self.send()
        self.assertEqual(response.status_code, 404)
