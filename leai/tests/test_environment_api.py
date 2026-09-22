from django.core import checks
from django.db import connection
from django.test import TestCase, override_settings


class EnvironmentApiTests(TestCase):
    endpoint = "/datapipeline/api/v1/environment/"

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
        response = self.client.post(self.endpoint)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Cache-Control"], "no-store")
