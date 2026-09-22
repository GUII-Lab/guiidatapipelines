import re
from functools import wraps

from django.conf import settings
from django.db import DatabaseError, connection
from django.http import HttpResponseNotAllowed, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt


CONTRACT_VERSION = "2026-09-21"
ENVIRONMENT_SCHEMA = {"local": "public", "qa": "leai_qa", "production": "public"}
ENVIRONMENT_APP_BASES = {
    "local": ["/"],
    "qa": ["/LEAI/qa/"],
    "production": ["/LEAI/"],
}
BUILD_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


def valid_build_identity(environment, build_id):
    if environment == "local":
        return build_id == "local-backend"
    return isinstance(build_id, str) and BUILD_SHA_PATTERN.fullmatch(build_id) is not None


def no_store_json(payload, status=200):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


def verified_environment_identity():
    environment = settings.LEAI_ENVIRONMENT
    build_id = settings.LEAI_BUILD_ID
    if environment not in ENVIRONMENT_SCHEMA or not valid_build_identity(environment, build_id):
        return None

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_schema()")
            schema_identity = cursor.fetchone()[0]
    except DatabaseError:
        return None

    if schema_identity != ENVIRONMENT_SCHEMA[environment]:
        return None

    return {
        "environment": environment,
        "backend_build_sha": build_id,
        "schema_identity": schema_identity,
        "contract_version": CONTRACT_VERSION,
        "allowed_app_bases": ENVIRONMENT_APP_BASES[environment],
        "server_time": timezone.now().isoformat(),
    }


def environment_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if verified_environment_identity() is None:
            return no_store_json({"error": "environment_unavailable"}, status=503)
        return view(request, *args, **kwargs)

    return wrapped


@csrf_exempt
def environment_view(request):
    if request.method != "GET":
        response = HttpResponseNotAllowed(["GET"])
        response["Cache-Control"] = "no-store"
        return response
    identity = verified_environment_identity()
    if identity is None:
        return no_store_json({"error": "environment_unavailable"}, status=503)
    return no_store_json(identity)
