from django.apps import AppConfig


class LeaiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "leai"

    def ready(self):
        from . import checks  # noqa: F401
