"""Exercise the actual WhiteNoise/Django stack, not a frontend dev server."""
from pathlib import Path
import json
from tempfile import TemporaryDirectory

from django.test import Client, SimpleTestCase, override_settings
from django.core.exceptions import ImproperlyConfigured
from leai.hosting import frontend_headers, validate_frontend_bundle


class FrontendHostingTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        (root / "assets").mkdir()
        (root / "assets/app-Abcd1234.js").write_text("export const ready = true;")
        for name in ("index.html", "feedback.html", "InstructorLogin.html"):
            (root / name).write_text("<!doctype html><title>LEAI</title><div id='root'></div>")
        self.settings_override = override_settings(
            LEAI_SERVE_FRONTEND=True, ROOT_URLCONF="leai.web_urls",
            WHITENOISE_ROOT=str(root), WHITENOISE_INDEX_FILE=True,
            WHITENOISE_AUTOREFRESH=True,
            WHITENOISE_ADD_HEADERS_FUNCTION=frontend_headers,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.client = Client()

    def test_html_and_assets_share_one_origin_and_html_is_not_cached(self):
        for url in ("/", "/feedback.html?id=test", "/InstructorLogin.html"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertIn(b"LEAI", b"".join(response.streaming_content))
        script = self.client.get("/assets/app-Abcd1234.js")
        self.assertEqual(script.status_code, 200)

    def test_unknown_api_and_retired_routes_do_not_fall_back_to_html(self):
        for url in ("/datapipeline/api/v1/unknown/", "/datapipeline/api/getOAI/", "/.env", "/missing.html"):
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_bundle_must_match_backend_and_be_a_clean_release(self):
        root = Path(self.directory.name)
        manifest = {"environment": "qa", "backendBuildSha": "b" * 40,
                    "basePath": "/", "apiBaseUrl": "/datapipeline/api/v1/", "workingTreeDirty": False}
        path = root / "leai-build-manifest.json"
        path.write_text(json.dumps(manifest))
        validate_frontend_bundle(root, "qa", "b" * 40)
        with self.assertRaises(ImproperlyConfigured):
            validate_frontend_bundle(root, "production", "b" * 40)
        with self.assertRaises(ImproperlyConfigured):
            validate_frontend_bundle(root, "qa", "c" * 40)
        for dirty in (True, None):
            path.write_text(json.dumps({**manifest, "workingTreeDirty": dirty}))
            with self.assertRaises(ImproperlyConfigured):
                validate_frontend_bundle(root, "qa", "b" * 40)
