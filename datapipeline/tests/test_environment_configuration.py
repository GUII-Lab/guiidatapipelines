import json
import os
import subprocess
import sys
import traceback
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEAI_ENVIRONMENT_VARIABLES = (
    'LEAI_ENV',
    'LEAI_DB_SCHEMA',
    'LEAI_BUILD_ID',
    'LEAI_EMAIL_ENABLED',
    'LEAI_ALLOWED_HOSTS',
    'LEAI_ALLOWED_ORIGINS',
    'SECRET_KEY',
)

STRONG_TEST_SECRET_KEY = (
    'task-3-test-only-secret-key-with-more-than-fifty-safe-characters-123456789'
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
from django.core.checks import WARNING, run_checks

django.setup()
issues = run_checks(include_deployment_checks=True)
payload = {
    'environment': settings.LEAI_ENV,
    'build_id': settings.LEAI_BUILD_ID,
    'email_enabled': settings.LEAI_EMAIL_ENABLED,
    'allowed_hosts': settings.ALLOWED_HOSTS,
    'leai_allowed_hosts': settings.LEAI_ALLOWED_HOSTS,
    'cors_allow_all_origins': settings.CORS_ALLOW_ALL_ORIGINS,
    'cors_allowed_origins': settings.CORS_ALLOWED_ORIGINS,
    'csrf_trusted_origins': settings.CSRF_TRUSTED_ORIGINS,
    'secure_ssl_redirect': settings.SECURE_SSL_REDIRECT,
    'secure_hsts_seconds': settings.SECURE_HSTS_SECONDS,
    'secure_hsts_include_subdomains': settings.SECURE_HSTS_INCLUDE_SUBDOMAINS,
    'secure_hsts_preload': settings.SECURE_HSTS_PRELOAD,
    'session_cookie_secure': settings.SESSION_COOKIE_SECURE,
    'csrf_cookie_secure': settings.CSRF_COOKIE_SECURE,
    'database_schema': getattr(settings, 'LEAI_DB_SCHEMA', None),
    'database_options': settings.DATABASES['default'].get('OPTIONS', {}),
    'leai_check_ids': sorted(
        issue.id for issue in issues
        if issue.id.startswith('leai.')
    ),
    'deployment_issue_ids': sorted(
        issue.id for issue in issues if issue.level >= WARNING
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
        self.assertEqual(loaded['database_schema'], 'public')
        self.assertEqual(
            loaded['database_options']['options'],
            '-c search_path=public',
        )
        self.assertIs(loaded['email_enabled'], False)
        self.assertIn('localhost', loaded['allowed_hosts'])
        self.assertIn('testserver', loaded['allowed_hosts'])
        self.assertIs(loaded['cors_allow_all_origins'], True)
        self.assertEqual(loaded['leai_check_ids'], [])

    def test_qa_uses_only_explicit_hosts_and_origins(self):
        loaded = self.load_settings(
            LEAI_ENV='qa',
            LEAI_DB_SCHEMA='leai_qa',
            LEAI_BUILD_ID='abc1234',
            LEAI_EMAIL_ENABLED='false',
            LEAI_ALLOWED_HOSTS='qa-api.example,qa-alt.example',
            LEAI_ALLOWED_ORIGINS='https://qa.example,https://review.example',
            SECRET_KEY=STRONG_TEST_SECRET_KEY,
        )

        self.assertEqual(loaded['environment'], 'qa')
        self.assertEqual(loaded['database_schema'], 'leai_qa')
        self.assertEqual(
            loaded['database_options']['options'],
            '-c search_path=leai_qa',
        )
        self.assertEqual(loaded['build_id'], 'abc1234')
        self.assertIs(loaded['email_enabled'], False)
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
        self.assertIs(loaded['secure_ssl_redirect'], True)
        self.assertGreater(loaded['secure_hsts_seconds'], 0)
        self.assertIs(loaded['secure_hsts_include_subdomains'], True)
        self.assertIs(loaded['secure_hsts_preload'], True)
        self.assertIs(loaded['session_cookie_secure'], True)
        self.assertIs(loaded['csrf_cookie_secure'], True)
        self.assertEqual(loaded['leai_check_ids'], [])
        self.assertEqual(loaded['deployment_issue_ids'], [])

    def test_production_also_disables_allow_all_cors(self):
        loaded = self.load_settings(
            LEAI_ENV='production',
            LEAI_DB_SCHEMA='public',
            LEAI_BUILD_ID='release-2026.09.13',
            LEAI_EMAIL_ENABLED='true',
            LEAI_ALLOWED_HOSTS='api.example.edu',
            LEAI_ALLOWED_ORIGINS='https://example.edu',
            SECRET_KEY=STRONG_TEST_SECRET_KEY,
        )

        self.assertEqual(loaded['environment'], 'production')
        self.assertEqual(loaded['database_schema'], 'public')
        self.assertEqual(
            loaded['database_options']['options'],
            '-c search_path=public',
        )
        self.assertIs(loaded['email_enabled'], True)
        self.assertIs(loaded['cors_allow_all_origins'], False)
        self.assertEqual(loaded['cors_allowed_origins'], ['https://example.edu'])
        self.assertEqual(loaded['csrf_trusted_origins'], ['https://example.edu'])
        self.assertEqual(loaded['leai_check_ids'], [])
        self.assertEqual(loaded['deployment_issue_ids'], [])

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

    def test_wildcard_style_nonlocal_hosts_are_system_check_errors(self):
        for host in ('.example.com', '*.example.com', '*'):
            with self.subTest(host=host):
                loaded = self.load_settings(
                    LEAI_ENV='qa',
                    LEAI_BUILD_ID='abc1234',
                    LEAI_EMAIL_ENABLED='false',
                    LEAI_ALLOWED_HOSTS=host,
                    LEAI_ALLOWED_ORIGINS='https://qa.example',
                )

                self.assertIn('leai.E005', loaded['leai_check_ids'])

    def test_wildcard_and_malformed_nonlocal_origins_are_system_check_errors(self):
        for origin in (
            'https://*.example.com',
            'https://*',
            'https://qa.example/path',
        ):
            with self.subTest(origin=origin):
                loaded = self.load_settings(
                    LEAI_ENV='qa',
                    LEAI_BUILD_ID='abc1234',
                    LEAI_EMAIL_ENABLED='false',
                    LEAI_ALLOWED_HOSTS='qa-api.example',
                    LEAI_ALLOWED_ORIGINS=origin,
                )

                self.assertIn('leai.E007', loaded['leai_check_ids'])

    def test_qa_email_enabled_is_a_system_check_error(self):
        loaded = self.load_settings(
            LEAI_ENV='qa',
            LEAI_BUILD_ID='abc1234',
            LEAI_EMAIL_ENABLED='true',
            LEAI_ALLOWED_HOSTS='qa-api.example',
            LEAI_ALLOWED_ORIGINS='https://qa.example',
            SECRET_KEY=STRONG_TEST_SECRET_KEY,
        )

        self.assertIn('leai.E012', loaded['leai_check_ids'])

    def test_qa_schema_misconfiguration_uses_only_non_application_fallback(self):
        cases = (
            {},
            {'LEAI_DB_SCHEMA': 'public'},
            {'LEAI_DB_SCHEMA': 'leai_qa,public'},
            {'LEAI_DB_SCHEMA': 'leai_qa public'},
            {'LEAI_DB_SCHEMA': 'not-a-schema'},
        )
        for schema_environment in cases:
            with self.subTest(schema_environment=schema_environment):
                loaded = self.load_settings(
                    LEAI_ENV='qa',
                    LEAI_BUILD_ID='abc1234',
                    LEAI_EMAIL_ENABLED='false',
                    LEAI_ALLOWED_HOSTS='qa-api.example',
                    LEAI_ALLOWED_ORIGINS='https://qa.example',
                    SECRET_KEY=STRONG_TEST_SECRET_KEY,
                    **schema_environment,
                )

                self.assertIn('leai.E013', loaded['leai_check_ids'])
                self.assertEqual(
                    loaded['database_options']['options'],
                    '-c search_path=pg_catalog',
                )
                self.assertNotIn(',public', loaded['database_options']['options'])
                self.assertNotIn(
                    'leai_qa public',
                    loaded['database_options']['options'],
                )

    @override_settings(
        LEAI_ENV='qa',
        LEAI_DB_SCHEMA='leai_qa',
        DATABASES={
            'default': {
                **settings.DATABASES['default'],
                'OPTIONS': {'options': '-c search_path=pg_catalog'},
            },
        },
    )
    def test_database_option_mismatch_has_a_stable_check_id(self):
        from guiidatapipelines.settings import check_leai_environment_configuration

        issue_ids = {
            issue.id
            for issue in check_leai_environment_configuration(None)
        }

        self.assertIn('leai.E014', issue_ids)

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
        LEAI_DB_SCHEMA='leai_qa',
        LEAI_BUILD_ID='abc1234',
        LEAI_EMAIL_ENABLED=False,
    )
    @patch(
        'datapipeline.environment_views.require_environment_database_schema',
        return_value='leai_qa',
        create=True,
    )
    def test_environment_endpoint_reports_only_safe_identity(self, schema_guard):
        response = self.client.get('/datapipeline/api/environment/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'environment': 'qa',
            'build_id': 'abc1234',
            'email_enabled': False,
            'database_schema': 'leai_qa',
        })
        schema_guard.assert_called_once_with('qa')
        self.assertNotContains(response, 'DATABASE_URL')
        self.assertNotContains(response, 'SECRET_KEY')

    @override_settings(
        LEAI_ENV='qa',
        LEAI_DB_SCHEMA='leai_qa',
        LEAI_BUILD_ID='postgres://must-not-leak',
        LEAI_EMAIL_ENABLED=False,
    )
    @patch(
        'datapipeline.environment_views.require_environment_database_schema',
        return_value='leai_qa',
        create=True,
    )
    def test_environment_endpoint_does_not_echo_an_unsafe_build_id(self, schema_guard):
        response = self.client.get('/datapipeline/api/environment/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['build_id'], '')
        self.assertEqual(response.json()['database_schema'], 'leai_qa')
        schema_guard.assert_called_once_with('qa')
        self.assertNotContains(response, 'postgres://must-not-leak')

    @override_settings(
        LEAI_ENV='qa',
        LEAI_DB_SCHEMA='leai_qa',
        LEAI_BUILD_ID='abc1234',
        LEAI_EMAIL_ENABLED=False,
    )
    def test_environment_endpoint_returns_safe_503_when_schema_is_unavailable(self):
        try:
            from datapipeline.database_schema import DatabaseSchemaError
        except ImportError:
            self.fail('database schema verification helper must exist')
        with patch(
            'datapipeline.environment_views.require_environment_database_schema',
            side_effect=DatabaseSchemaError(
                'DATABASE_URL=postgres://secret-bearing-error',
            ),
            create=True,
        ):
            response = self.client.get('/datapipeline/api/environment/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            'error': 'Database schema verification failed.',
        })
        self.assertNotContains(
            response,
            'secret-bearing-error',
            status_code=503,
        )

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
        'LEAI_DB_SCHEMA': 'leai_qa',
        'LEAI_BUILD_ID': 'abc1234',
        'LEAI_EMAIL_ENABLED': False,
        'LEAI_ALLOWED_HOSTS': ['qa-api.example'],
        'ALLOWED_HOSTS': ['qa-api.example'],
        'LEAI_ALLOWED_ORIGINS': ['https://qa.example'],
        'CORS_ALLOW_ALL_ORIGINS': False,
        'CORS_ALLOWED_ORIGINS': ['https://qa.example'],
        'CSRF_TRUSTED_ORIGINS': ['https://qa.example'],
        'DEBUG': False,
        'SECRET_KEY': STRONG_TEST_SECRET_KEY,
        'SECURE_SSL_REDIRECT': True,
        'SECURE_HSTS_SECONDS': 31536000,
        'SECURE_HSTS_INCLUDE_SUBDOMAINS': True,
        'SECURE_HSTS_PRELOAD': True,
        'SESSION_COOKIE_SECURE': True,
        'CSRF_COOKIE_SECURE': True,
        'DATABASES': {
            'default': {
                **settings.DATABASES['default'],
                'OPTIONS': {'options': '-c search_path=leai_qa'},
            },
        },
    }

    def test_verify_command_rejects_environment_mismatch(self):
        with override_settings(LEAI_ENV='production'):
            with self.assertRaises(CommandError):
                call_command('verify_leai_environment', expect='qa')

    @override_settings(**valid_qa_settings)
    @patch(
        'datapipeline.management.commands.verify_leai_environment.require_environment_database_schema',
        return_value='leai_qa',
        create=True,
    )
    def test_verify_command_prints_only_safe_json_after_schema_verification(
        self,
        schema_guard,
    ):
        stdout = StringIO()
        stderr = StringIO()

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
            'database_schema': 'leai_qa',
        })
        self.assertEqual(stderr.getvalue(), '')
        self.assertNotIn('DATABASE_URL', stdout.getvalue())
        self.assertNotIn(settings.SECRET_KEY, stdout.getvalue())
        schema_guard.assert_called_once_with('qa')

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

    def test_verify_command_fails_on_nonlocal_deployment_warnings(self):
        warning_cases = (
            ({'SECURE_HSTS_PRELOAD': False}, 'security.W021'),
            ({'SECRET_KEY': 'weak'}, 'security.W009'),
        )
        for changed_settings, expected_warning in warning_cases:
            with self.subTest(expected_warning=expected_warning):
                with override_settings(**{
                    **self.valid_qa_settings,
                    **changed_settings,
                }):
                    with self.assertRaisesRegex(CommandError, expected_warning):
                        call_command(
                            'verify_leai_environment',
                            expect='qa',
                            json=True,
                        )

    @override_settings(**{
        **valid_qa_settings,
        'LEAI_ENV': 'production',
        'LEAI_DB_SCHEMA': 'public',
        'LEAI_EMAIL_ENABLED': True,
        'DATABASES': {
            'default': {
                **settings.DATABASES['default'],
                'OPTIONS': {'options': '-c search_path=public'},
            },
        },
    })
    def test_secure_production_verification_can_enable_email(self):
        stdout = StringIO()

        call_command(
            'verify_leai_environment',
            expect='production',
            json=True,
            stdout=stdout,
        )

        self.assertEqual(json.loads(stdout.getvalue()), {
            'environment': 'production',
            'build_id': 'abc1234',
            'email_enabled': True,
            'database_schema': 'public',
        })

    @override_settings(**valid_qa_settings)
    def test_database_failure_suppresses_secret_bearing_cause(self):
        try:
            from datapipeline.database_schema import DatabaseSchemaError
        except ImportError:
            self.fail('database schema verification helper must exist')
        with self.assertRaises(CommandError) as raised:
            with patch(
                'datapipeline.management.commands.verify_leai_environment.require_environment_database_schema',
                side_effect=DatabaseSchemaError(
                    'DATABASE_URL=postgres://secret-bearing-error',
                ),
                create=True,
            ):
                call_command('verify_leai_environment', expect='qa', json=True)

        error = raised.exception
        rendered_traceback = ''.join(traceback.format_exception(error))
        self.assertEqual(str(error), 'Database schema verification failed.')
        self.assertIsNone(error.__cause__)
        self.assertIs(error.__suppress_context__, True)
        self.assertNotIn('secret-bearing-error', rendered_traceback)
