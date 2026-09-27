import os
import subprocess
import sys

from django.core import checks
from django.db import connection
from django.test import Client, TestCase, override_settings


class EnvironmentApiTests(TestCase):
    endpoint = "/datapipeline/api/v1/environment/"

    def test_hosted_runtime_without_identity_cannot_claim_local(self):
        environment = os.environ.copy()
        environment.pop("LEAI_ENVIRONMENT", None)
        environment.pop("LEAI_BUILD_ID", None)
        environment["DYNO"] = "web.1"
        environment["SECRET_KEY"] = "test-only-" + "abcdef1234" * 6
        environment["DJANGO_ALLOWED_HOSTS"] = "leai-qa-example.herokuapp.com"
        environment["DJANGO_SETTINGS_MODULE"] = "guiidatapipelines.settings"
        result = subprocess.run(
            [sys.executable, "-c", "import json; from django.conf import settings; print(json.dumps([settings.LEAI_ENVIRONMENT, settings.LEAI_BUILD_ID]))"],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Hosted runtime requires LEAI_ENVIRONMENT", result.stderr)

    @override_settings(LEAI_ENVIRONMENT="local", LEAI_BUILD_ID="local-backend")
    def test_local_handshake_reports_verified_schema_and_no_store(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_schema()")
            actual_schema = cursor.fetchone()[0]

        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(
            set(response.json()),
            {
                "environment",
                "backend_build_sha",
                "schema_identity",
                "contract_version",
                "allowed_app_bases",
                "server_time",
            },
        )
        self.assertEqual(response.json()["environment"], "local")
        self.assertEqual(response.json()["schema_identity"], actual_schema)
        self.assertEqual(response.json()["allowed_app_bases"], ["/"])
        self.assertEqual(response.json()["contract_version"], "2026-09-21")

    @override_settings(LEAI_ENVIRONMENT="qa", LEAI_BUILD_ID="a" * 40)
    def test_qa_handshake_fails_closed_when_database_schema_is_not_qa(self):
        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {"error": "environment_unavailable"})

    @override_settings(LEAI_ENVIRONMENT="qa", LEAI_BUILD_ID="not-a-sha")
    def test_qa_handshake_fails_closed_when_build_identity_is_invalid(self):
        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {"error": "environment_unavailable"})

    @override_settings(LEAI_ENVIRONMENT="qa", LEAI_BUILD_ID="not-a-sha")
    def test_qa_configuration_fails_startup_checks_with_invalid_build_identity(self):
        errors = checks.run_checks()
        self.assertIn("leai.E001", {error.id for error in errors})

    @override_settings(LEAI_ENVIRONMENT="local", LEAI_BUILD_ID="local-backend")
    def test_unsupported_method_is_not_cached(self):
        response = Client(enforce_csrf_checks=True).post(self.endpoint)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Cache-Control"], "no-store")
