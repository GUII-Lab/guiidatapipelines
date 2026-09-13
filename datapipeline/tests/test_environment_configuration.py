import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEAI_ENVIRONMENT_VARIABLES = (
    'LEAI_ENV',
    'LEAI_BUILD_ID',
    'LEAI_EMAIL_ENABLED',
    'LEAI_ALLOWED_HOSTS',
    'LEAI_ALLOWED_ORIGINS',
)


class EnvironmentSettingsTests(SimpleTestCase):
    def load_settings(self, **environment):
        child_environment = os.environ.copy()
        for name in LEAI_ENVIRONMENT_VARIABLES:
            child_environment.pop(name, None)
        child_environment.update(environment)
        script = """
import json
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'guiidatapipelines.settings')

import django
from django.conf import settings
from django.core.checks import run_checks

django.setup()
payload = {
    'environment': settings.LEAI_ENV,
    'build_id': settings.LEAI_BUILD_ID,
    'email_enabled': settings.LEAI_EMAIL_ENABLED,
    'allowed_hosts': settings.ALLOWED_HOSTS,
    'leai_allowed_hosts': settings.LEAI_ALLOWED_HOSTS,
    'cors_allow_all_origins': settings.CORS_ALLOW_ALL_ORIGINS,
    'cors_allowed_origins': settings.CORS_ALLOWED_ORIGINS,
    'csrf_trusted_origins': settings.CSRF_TRUSTED_ORIGINS,
    'leai_check_ids': sorted(
        issue.id for issue in run_checks(include_deployment_checks=True)
        if issue.id.startswith('leai.')
    ),
}
print(json.dumps(payload))
"""
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=PROJECT_ROOT,
            env=child_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def test_local_defaults_remain_usable(self):
        loaded = self.load_settings()

        self.assertEqual(loaded['environment'], 'local')
        self.assertEqual(loaded['build_id'], 'local')
        self.assertIs(loaded['email_enabled'], False)
        self.assertIn('localhost', loaded['allowed_hosts'])
        self.assertIn('testserver', loaded['allowed_hosts'])
        self.assertIs(loaded['cors_allow_all_origins'], True)
        self.assertEqual(loaded['leai_check_ids'], [])

    def test_qa_uses_only_explicit_hosts_and_origins(self):
        loaded = self.load_settings(
            LEAI_ENV='qa',
            LEAI_BUILD_ID='abc1234',
            LEAI_EMAIL_ENABLED='true',
            LEAI_ALLOWED_HOSTS='qa-api.example,qa-alt.example',
            LEAI_ALLOWED_ORIGINS='https://qa.example,https://review.example',
        )

        self.assertEqual(loaded['environment'], 'qa')
        self.assertEqual(loaded['build_id'], 'abc1234')
        self.assertIs(loaded['email_enabled'], True)
        self.assertEqual(
            loaded['allowed_hosts'],
            ['qa-api.example', 'qa-alt.example'],
        )
        self.assertEqual(loaded['allowed_hosts'], loaded['leai_allowed_hosts'])
        self.assertIs(loaded['cors_allow_all_origins'], False)
        self.assertEqual(
            loaded['cors_allowed_origins'],
            ['https://qa.example', 'https://review.example'],
        )
        self.assertEqual(
            loaded['csrf_trusted_origins'],
            ['https://qa.example', 'https://review.example'],
        )
        self.assertEqual(loaded['leai_check_ids'], [])

    def test_production_also_disables_allow_all_cors(self):
        loaded = self.load_settings(
            LEAI_ENV='production',
            LEAI_BUILD_ID='release-2026.09.13',
            LEAI_EMAIL_ENABLED='false',
            LEAI_ALLOWED_HOSTS='api.example.edu',
            LEAI_ALLOWED_ORIGINS='https://example.edu',
        )

        self.assertEqual(loaded['environment'], 'production')
        self.assertIs(loaded['cors_allow_all_origins'], False)
        self.assertEqual(loaded['cors_allowed_origins'], ['https://example.edu'])
        self.assertEqual(loaded['csrf_trusted_origins'], ['https://example.edu'])
        self.assertEqual(loaded['leai_check_ids'], [])

    def test_missing_nonlocal_hosts_and_origins_are_system_check_errors(self):
        loaded = self.load_settings(
            LEAI_ENV='qa',
            LEAI_BUILD_ID='abc1234',
            LEAI_EMAIL_ENABLED='false',
        )

        self.assertIn('leai.E004', loaded['leai_check_ids'])
        self.assertIn('leai.E006', loaded['leai_check_ids'])
        self.assertNotEqual(loaded['allowed_hosts'], ['*'])
        self.assertIs(loaded['cors_allow_all_origins'], False)

    def test_malformed_nonlocal_hosts_and_origins_are_system_check_errors(self):
        loaded = self.load_settings(
            LEAI_ENV='qa',
            LEAI_BUILD_ID='abc1234',
            LEAI_EMAIL_ENABLED='false',
            LEAI_ALLOWED_HOSTS='*',
            LEAI_ALLOWED_ORIGINS='https://qa.example/path',
        )

        self.assertIn('leai.E005', loaded['leai_check_ids'])
        self.assertIn('leai.E007', loaded['leai_check_ids'])

    def test_invalid_environment_email_flag_and_build_id_are_check_errors(self):
        loaded = self.load_settings(
            LEAI_ENV='staging',
            LEAI_BUILD_ID='secret/value',
            LEAI_EMAIL_ENABLED='sometimes',
        )

        self.assertIn('leai.E001', loaded['leai_check_ids'])
        self.assertIn('leai.E002', loaded['leai_check_ids'])
        self.assertIn('leai.E003', loaded['leai_check_ids'])
        self.assertEqual(loaded['build_id'], '')
        self.assertIs(loaded['email_enabled'], False)


class EnvironmentEndpointTests(SimpleTestCase):
    @override_settings(
        LEAI_ENV='qa',
        LEAI_BUILD_ID='abc1234',
        LEAI_EMAIL_ENABLED=False,
    )
    def test_environment_endpoint_reports_only_safe_identity(self):
        response = self.client.get('/datapipeline/api/environment/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'environment': 'qa',
            'build_id': 'abc1234',
            'email_enabled': False,
        })
        self.assertNotContains(response, 'DATABASE_URL')
        self.assertNotContains(response, 'SECRET_KEY')

    @override_settings(
        LEAI_ENV='qa',
        LEAI_BUILD_ID='postgres://must-not-leak',
        LEAI_EMAIL_ENABLED=False,
    )
    def test_environment_endpoint_does_not_echo_an_unsafe_build_id(self):
        response = self.client.get('/datapipeline/api/environment/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['build_id'], '')
        self.assertNotContains(response, 'postgres://must-not-leak')

    def test_environment_endpoint_rejects_non_get_methods(self):
        for method_name in ('post', 'put', 'patch', 'delete'):
            with self.subTest(method=method_name):
                response = getattr(self.client, method_name)(
                    '/datapipeline/api/environment/',
                )
                self.assertEqual(response.status_code, 405)


class VerifyEnvironmentCommandTests(TestCase):
    valid_qa_settings = {
        'LEAI_ENV': 'qa',
        'LEAI_BUILD_ID': 'abc1234',
        'LEAI_EMAIL_ENABLED': False,
        'LEAI_ALLOWED_HOSTS': ['qa-api.example'],
        'ALLOWED_HOSTS': ['qa-api.example'],
        'LEAI_ALLOWED_ORIGINS': ['https://qa.example'],
        'CORS_ALLOW_ALL_ORIGINS': False,
        'CORS_ALLOWED_ORIGINS': ['https://qa.example'],
        'CSRF_TRUSTED_ORIGINS': ['https://qa.example'],
    }

    def test_verify_command_rejects_environment_mismatch(self):
        with override_settings(LEAI_ENV='production'):
            with self.assertRaises(CommandError):
                call_command('verify_leai_environment', expect='qa')

    @override_settings(**valid_qa_settings)
    def test_verify_command_prints_only_safe_json_and_queries_default_database(self):
        stdout = StringIO()
        stderr = StringIO()

        with CaptureQueriesContext(connection) as captured_queries:
            call_command(
                'verify_leai_environment',
                expect='qa',
                json=True,
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(json.loads(stdout.getvalue()), {
            'environment': 'qa',
            'build_id': 'abc1234',
            'email_enabled': False,
        })
        self.assertEqual(stderr.getvalue(), '')
        self.assertNotIn('DATABASE_URL', stdout.getvalue())
        self.assertNotIn(settings.SECRET_KEY, stdout.getvalue())
        self.assertTrue(any(
            query['sql'].strip().upper() == 'SELECT 1'
            for query in captured_queries.captured_queries
        ))

    @override_settings(
        LEAI_ENV='qa',
        LEAI_BUILD_ID='abc1234',
        LEAI_EMAIL_ENABLED=False,
        LEAI_ALLOWED_HOSTS=[],
        ALLOWED_HOSTS=[],
        LEAI_ALLOWED_ORIGINS=[],
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOWED_ORIGINS=[],
        CSRF_TRUSTED_ORIGINS=[],
    )
    def test_verify_command_fails_when_deployment_checks_have_errors(self):
        with self.assertRaisesRegex(CommandError, 'leai.E004'):
            call_command('verify_leai_environment', expect='qa', json=True)
