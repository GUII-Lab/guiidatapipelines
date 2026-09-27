"""Public build headers for the opt-in same-origin Heroku release."""
import json
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def frontend_headers(headers, path, url):
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    # HTML/manifests must not pin a browser to an old environment or bundle.
    if Path(path).suffix in {".html", ".json"}:
        headers["Cache-Control"] = "no-store"


def validate_frontend_bundle(root, environment, backend_build_id):
    try:
        manifest = json.loads((Path(root) / "leai-build-manifest.json").read_text())
    except (OSError, ValueError) as error:
        raise ImproperlyConfigured("Same-origin hosting requires a verified frontend bundle") from error
    if (manifest.get("environment") != environment
            or manifest.get("backendBuildSha") != backend_build_id
            or manifest.get("basePath") != "/"
            or manifest.get("apiBaseUrl") != "/datapipeline/api/v1/"):
        raise ImproperlyConfigured("Frontend/backend release identities do not match")
    if environment != "local" and manifest.get("workingTreeDirty") is not False:
        raise ImproperlyConfigured("Hosted frontend must be built from a clean committed source")
    if not (Path(root) / "InstructorLogin.html").is_file():
        raise ImproperlyConfigured("Frontend login entry point is missing")
