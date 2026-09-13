import re

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from datapipeline.database_schema import (
    DatabaseSchemaError,
    require_environment_database_schema,
)

_ENVIRONMENTS = frozenset({'local', 'qa', 'production'})
_SAFE_BUILD_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


def environment_identity(*, database_schema):
    environment = str(getattr(settings, 'LEAI_ENV', '') or '')
    build_id = str(getattr(settings, 'LEAI_BUILD_ID', '') or '')
    return {
        'environment': environment if environment in _ENVIRONMENTS else 'unknown',
        'build_id': build_id if _SAFE_BUILD_ID.fullmatch(build_id) else '',
        'email_enabled': getattr(settings, 'LEAI_EMAIL_ENABLED', False) is True,
        'database_schema': database_schema,
    }


@require_GET
def environment(request):
    try:
        database_schema = require_environment_database_schema(settings.LEAI_ENV)
    except DatabaseSchemaError:
        return JsonResponse(
            {'error': 'Database schema verification failed.'},
            status=503,
        )
    return JsonResponse(environment_identity(database_schema=database_schema))
