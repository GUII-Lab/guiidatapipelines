from django.conf import settings
from django.core import checks

from .api.environment import ENVIRONMENT_SCHEMA, valid_build_identity


@checks.register()
def check_leai_environment_identity(app_configs, **_kwargs):
    environment = settings.LEAI_ENVIRONMENT
    if environment not in ENVIRONMENT_SCHEMA or not valid_build_identity(
        environment, settings.LEAI_BUILD_ID
    ):
        return [checks.Error(
            "LEAI deployment environment or build identity is invalid",
            id="leai.E001",
        )]
    return []
