from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound


def page_not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    """Return the built LEAI not-found document for browser routes only."""
    if request.path.startswith("/datapipeline/api/"):
        return HttpResponseNotFound("Not found")

    frontend_root = getattr(settings, "LEAI_FRONTEND_ROOT", None)
    page = Path(frontend_root, "NotFound.html") if frontend_root else None
    if page is None or not page.is_file():
        return HttpResponseNotFound("Not found")

    return HttpResponse(page.read_bytes(), status=404, content_type="text/html; charset=utf-8")
