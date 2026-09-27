import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured


class HostedSecuritySettingsTests(unittest.TestCase):
    settings_path = Path(__file__).with_name("settings.py")

    def test_hosted_runtime_uses_explicit_hosts_https_and_secure_cookies(self):
        environment = {
            "DYNO": "web.1",
            "HEROKU_APP_NAME": "leai-qa-example",
            "SECRET_KEY": "test-only-" + "abcdef1234" * 6,
            "DJANGO_ALLOWED_HOSTS": "leai-qa-example.herokuapp.com",
            "LEAI_ENVIRONMENT": "qa",
            "LEAI_DB_SCHEMA": "leai_qa",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = runpy.run_path(str(self.settings_path))

        self.assertFalse(settings["DEBUG"])
        self.assertEqual(settings["ALLOWED_HOSTS"], ["leai-qa-example.herokuapp.com"])
        self.assertTrue(settings["SECURE_SSL_REDIRECT"])
        self.assertEqual(settings["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertTrue(settings["SESSION_COOKIE_SECURE"])
        self.assertTrue(settings["CSRF_COOKIE_SECURE"])
        self.assertEqual(settings["SESSION_COOKIE_NAME"], "__Host-leai-session")
        self.assertIsNone(settings["SESSION_COOKIE_DOMAIN"])
        self.assertEqual(settings["SESSION_COOKIE_PATH"], "/")
        self.assertGreaterEqual(settings["SECURE_HSTS_SECONDS"], 31536000)

    def test_uses_the_actual_heroku_default_domain_not_an_invented_app_name(self):
        environment = {
            "DYNO": "web.1", "HEROKU_APP_NAME": "leai-qa-example",
            "HEROKU_APP_DEFAULT_DOMAIN_NAME": "leai-qa-example-123456.herokuapp.com",
            "SECRET_KEY": "test-only-" + "abcdef1234" * 6,
            "DJANGO_ALLOWED_HOSTS": "", "LEAI_ENVIRONMENT": "qa",
            "LEAI_DB_SCHEMA": "leai_qa",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = runpy.run_path(str(self.settings_path))
        self.assertEqual(settings["ALLOWED_HOSTS"], [environment["HEROKU_APP_DEFAULT_DOMAIN_NAME"]])

    def test_qa_database_connection_is_pinned_to_its_existing_schema(self):
        environment = {
            "DYNO": "web.1",
            "SECRET_KEY": "test-only-" + "abcdef1234" * 6,
            "DJANGO_ALLOWED_HOSTS": "leai-qa-example.herokuapp.com",
            "LEAI_ENVIRONMENT": "qa",
            "LEAI_DB_SCHEMA": "leai_qa",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = runpy.run_path(str(self.settings_path))

        self.assertEqual(
            settings["DATABASES"]["default"]["OPTIONS"]["options"],
            "-c search_path=leai_qa",
        )

    def test_qa_rejects_missing_or_wrong_schema_before_migration(self):
        base = {
            "DYNO": "web.1",
            "SECRET_KEY": "test-only-" + "abcdef1234" * 6,
            "DJANGO_ALLOWED_HOSTS": "leai-qa-example.herokuapp.com",
            "LEAI_ENVIRONMENT": "qa",
        }
        for schema in ("", "public", "leai_qa,public"):
            with self.subTest(schema=schema):
                with patch.dict(os.environ, {**base, "LEAI_DB_SCHEMA": schema}, clear=False):
                    with self.assertRaises(ImproperlyConfigured):
                        runpy.run_path(str(self.settings_path))

    def test_hosted_runtime_rejects_missing_environment_before_migration(self):
        environment = {
            "DYNO": "web.1",
            "SECRET_KEY": "test-only-" + "abcdef1234" * 6,
            "DJANGO_ALLOWED_HOSTS": "leai-qa-example.herokuapp.com",
            "LEAI_ENVIRONMENT": "",
            "LEAI_DB_SCHEMA": "leai_qa",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(ImproperlyConfigured):
                runpy.run_path(str(self.settings_path))

    def test_production_rejects_qa_schema(self):
        environment = {
            "DYNO": "web.1",
            "SECRET_KEY": "test-only-" + "abcdef1234" * 6,
            "DJANGO_ALLOWED_HOSTS": "leai-example.herokuapp.com",
            "LEAI_ENVIRONMENT": "production",
            "LEAI_DB_SCHEMA": "leai_qa",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(ImproperlyConfigured):
                runpy.run_path(str(self.settings_path))

    def test_hosted_runtime_rejects_missing_secret(self):
        environment = {
            "DYNO": "web.1",
            "HEROKU_APP_NAME": "leai-qa-example",
            "SECRET_KEY": "",
            "DJANGO_ALLOWED_HOSTS": "leai-qa-example.herokuapp.com",
            "LEAI_ENVIRONMENT": "qa",
            "LEAI_DB_SCHEMA": "leai_qa",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(ImproperlyConfigured):
                runpy.run_path(str(self.settings_path))

    def test_hosted_runtime_rejects_wildcard_hosts(self):
        environment = {
            "DYNO": "web.1",
            "HEROKU_APP_NAME": "leai-qa-example",
            "SECRET_KEY": "test-only-" + "abcdef1234" * 6,
            "DJANGO_ALLOWED_HOSTS": "*",
            "LEAI_ENVIRONMENT": "qa",
            "LEAI_DB_SCHEMA": "leai_qa",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(ImproperlyConfigured):
                runpy.run_path(str(self.settings_path))


if __name__ == "__main__":
    unittest.main()
