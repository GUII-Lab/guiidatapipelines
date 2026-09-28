import uuid

from django.db import models

from .identity import Course, InstructorAccount


class DomainJob(models.Model):
    """Small restart-safe job record; payloads contain canonical IDs only."""

    STATUSES = (("pending", "Pending"), ("running", "Running"), ("completed", "Completed"), ("failed", "Failed"))
    TYPES = (("feedback_chat_turn", "Feedback Chat turn"), ("authoring_ai_run", "Authoring AI run"))

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="domain_jobs")
    actor_account = models.ForeignKey(InstructorAccount, on_delete=models.PROTECT, related_name="domain_jobs")
    job_type = models.CharField(max_length=32, choices=TYPES)
    status = models.CharField(max_length=16, choices=STATUSES, default="pending")
    attempts = models.PositiveSmallIntegerField(default=0)
    payload = models.JSONField(default=dict)
    result = models.JSONField(null=True, blank=True)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(job_type__in=["feedback_chat_turn", "authoring_ai_run"]), name="leai_domain_job_type_valid"),
            models.CheckConstraint(check=models.Q(status__in=["pending", "running", "completed", "failed"]), name="leai_domain_job_status_valid"),
            models.CheckConstraint(check=models.Q(attempts__lte=5), name="leai_domain_job_attempts_bounded"),
            models.CheckConstraint(
                check=(models.Q(status="running", lease_token__isnull=False, lease_expires_at__isnull=False)
                       | models.Q(status__in=["pending", "completed", "failed"], lease_token__isnull=True, lease_expires_at__isnull=True)),
                name="leai_domain_job_lease_shape_valid",
            ),
        ]
