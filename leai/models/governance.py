import uuid

from django.db import models

from .identity import Course, InstructorAccount


class AuditEvent(models.Model):
    ACTOR_KINDS = (("instructor", "Instructor"), ("platform_admin", "Platform Admin"), ("system", "System"), ("import", "Import"))
    OUTCOMES = (("allowed", "Allowed"), ("denied", "Denied"), ("failed", "Failed"))

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    actor_account = models.ForeignKey(InstructorAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events")
    course = models.ForeignKey(Course, null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events")
    actor_kind = models.CharField(max_length=32, choices=ACTOR_KINDS)
    action = models.CharField(max_length=128)
    outcome = models.CharField(max_length=16, choices=OUTCOMES)
    target_type = models.CharField(max_length=64)
    target_id = models.CharField(max_length=128)
    request_id = models.CharField(max_length=128, blank=True, default="")
    bounded_metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(actor_kind__in=["instructor", "platform_admin", "system", "import"]), name="leai_audit_actor_kind_valid"),
            models.CheckConstraint(check=models.Q(outcome__in=["allowed", "denied", "failed"]), name="leai_audit_outcome_valid"),
            models.CheckConstraint(check=~models.Q(action=""), name="leai_audit_action_nonempty"),
            models.CheckConstraint(check=~models.Q(target_type=""), name="leai_audit_target_type_nonempty"),
            models.CheckConstraint(check=~models.Q(target_id=""), name="leai_audit_target_id_nonempty"),
        ]


class ImportRun(models.Model):
    RUN_KINDS = (("dry_run", "Dry run"), ("execute", "Execute"))
    STATUSES = (("pending", "Pending"), ("running", "Running"), ("completed", "Completed"), ("failed", "Failed"))

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    actor_account = models.ForeignKey(InstructorAccount, on_delete=models.PROTECT, related_name="import_runs")
    run_kind = models.CharField(max_length=16, choices=RUN_KINDS)
    target_environment = models.CharField(max_length=32)
    idempotency_key_hash = models.CharField(max_length=64)
    manifest_digest = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=16, choices=STATUSES, default="pending")
    source_environment = models.CharField(max_length=32)
    source_release = models.CharField(max_length=128)
    target_release = models.CharField(max_length=128)
    migration_set = models.CharField(max_length=255)
    contract_version = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("target_environment", "idempotency_key_hash"), name="leai_import_run_target_idempotency_uniq"),
            models.CheckConstraint(check=models.Q(run_kind__in=["dry_run", "execute"]), name="leai_import_run_kind_valid"),
            models.CheckConstraint(check=models.Q(status__in=["pending", "running", "completed", "failed"]), name="leai_import_run_status_valid"),
            models.CheckConstraint(check=models.Q(idempotency_key_hash__regex=r"^[0-9a-f]{64}$"), name="leai_import_run_idempotency_hash_valid"),
            models.CheckConstraint(check=models.Q(manifest_digest__regex=r"^[0-9a-f]{64}$"), name="leai_import_run_manifest_digest_valid"),
        ]


class ImportRecordMap(models.Model):
    id = models.BigAutoField(primary_key=True)
    first_import_run = models.ForeignKey(ImportRun, on_delete=models.PROTECT, related_name="first_created_record_maps")
    source_environment = models.CharField(max_length=32)
    target_environment = models.CharField(max_length=32)
    source_model = models.CharField(max_length=128)
    source_key = models.CharField(max_length=255)
    source_row_digest = models.CharField(max_length=64)
    target_model = models.CharField(max_length=128)
    target_key = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("source_environment", "target_environment", "source_model", "source_key"), name="leai_import_record_map_source_uniq"),
            models.CheckConstraint(check=models.Q(source_row_digest__regex=r"^[0-9a-f]{64}$"), name="leai_import_record_map_digest_valid"),
        ]


class ImportRecordOutcome(models.Model):
    DISPOSITIONS = (("mapped", "Mapped"), ("reused", "Reused"), ("quarantined", "Quarantined"), ("excluded", "Excluded"), ("regenerated", "Regenerated"))
    REASON_CODES = (("none", "None"), ("unresolved_owner", "Unresolved owner"), ("unresolved_course", "Unresolved course"), ("unresolved_session", "Unresolved session"), ("unresolved_scope", "Unresolved scope"), ("unknown_consent", "Unknown consent"), ("digest_conflict", "Digest conflict"), ("source_excluded", "Source excluded"), ("cache_regenerated", "Cache regenerated"))

    id = models.BigAutoField(primary_key=True)
    import_run = models.ForeignKey(ImportRun, on_delete=models.PROTECT, related_name="record_outcomes")
    import_record_map = models.ForeignKey(ImportRecordMap, null=True, blank=True, on_delete=models.PROTECT, related_name="reuse_outcomes")
    source_model = models.CharField(max_length=128)
    source_key = models.CharField(max_length=255)
    disposition = models.CharField(max_length=16, choices=DISPOSITIONS)
    reason_code = models.CharField(max_length=32, choices=REASON_CODES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("import_run", "source_model", "source_key"), name="leai_import_outcome_run_source_uniq"),
            models.CheckConstraint(check=models.Q(disposition__in=["mapped", "reused", "quarantined", "excluded", "regenerated"]), name="leai_import_outcome_disposition_valid"),
            models.CheckConstraint(check=models.Q(reason_code__in=["none", "unresolved_owner", "unresolved_course", "unresolved_session", "unresolved_scope", "unknown_consent", "digest_conflict", "source_excluded", "cache_regenerated"]), name="leai_import_outcome_reason_valid"),
            models.CheckConstraint(check=~models.Q(disposition="quarantined", reason_code="none"), name="leai_import_outcome_quarantine_reason_required"),
        ]
