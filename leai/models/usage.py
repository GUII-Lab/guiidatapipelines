import uuid

from django.db import models

from .responses import ResponseSession


class ProductUsageEvent(models.Model):
    class EventType(models.TextChoices):
        SURVEY_LOADED = "survey_loaded"
        OUTPUT_DOWNLOAD_ATTEMPTED = "output_download_attempted"

    class ResponsePhase(models.TextChoices):
        FIRST_LOAD = "first_load"
        RELOAD_BEFORE_RESPONSE = "reload_before_response"
        RESUME_DURING_RESPONSE = "resume_during_response"
        RELOAD_AFTER_COMPLETION = "reload_after_completion"

    class OutputKind(models.TextChoices):
        CERTIFICATE = "certificate"
        COMPLETED_RESPONSE = "completed_response"

    class Outcome(models.TextChoices):
        SUCCEEDED = "succeeded"
        NOT_READY = "not_ready"
        CLOSED = "closed"
        GENERATION_FAILED = "generation_failed"

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    response_session = models.ForeignKey(
        ResponseSession, on_delete=models.PROTECT, related_name="usage_events",
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    event_version = models.PositiveSmallIntegerField()
    response_phase = models.CharField(
        max_length=32, choices=ResponsePhase.choices, null=True, blank=True,
    )
    output_kind = models.CharField(
        max_length=32, choices=OutputKind.choices, null=True, blank=True,
    )
    outcome = models.CharField(
        max_length=32, choices=Outcome.choices, null=True, blank=True,
    )
    app_build_sha = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(event_version=1),
                name="leai_usage_event_version_one",
            ),
            models.CheckConstraint(
                check=models.Q(app_build_sha__regex=r"^[0-9a-f]{7,64}$"),
                name="leai_usage_event_build_sha_valid",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(
                        event_type="survey_loaded",
                        response_phase__in=[
                            "first_load", "reload_before_response",
                            "resume_during_response", "reload_after_completion",
                        ],
                        response_phase__isnull=False,
                        output_kind__isnull=True,
                        outcome__isnull=True,
                    )
                    | models.Q(
                        event_type="output_download_attempted",
                        response_phase__isnull=True,
                        output_kind__in=["certificate", "completed_response"],
                        output_kind__isnull=False,
                        outcome__in=[
                            "succeeded", "not_ready", "closed", "generation_failed",
                        ],
                        outcome__isnull=False,
                    )
                ),
                name="leai_usage_event_exact_shape",
            ),
        ]
