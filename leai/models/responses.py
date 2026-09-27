import uuid

from django.db import models
from django.db.models.lookups import Exact, LessThanOrEqual

from .authoring import SurveyOccurrence
from .identity import Course, InstructorAccount


MAX_MUTATION_RESULT_BYTES = 16 * 1024


class TeamConfiguration(models.Model):
    id = models.BigAutoField(primary_key=True)
    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name="team_configurations",
    )
    name = models.CharField(max_length=200)
    settings_version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="created_team_configurations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("id", "course"),
                name="leai_team_configuration_id_course_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(settings_version__gte=1),
                name="leai_team_configuration_settings_version_positive",
            ),
        ]


class TeamDefinition(models.Model):
    id = models.BigAutoField(primary_key=True)
    configuration = models.ForeignKey(
        TeamConfiguration,
        on_delete=models.PROTECT,
        related_name="definitions",
    )
    stable_key = models.CharField(max_length=64)
    label = models.CharField(max_length=200)
    sort_order = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("configuration", "stable_key"),
                name="leai_team_definition_stable_key_uniq",
            ),
            models.UniqueConstraint(
                fields=("configuration", "sort_order"),
                name="leai_team_definition_sort_order_uniq",
            ),
        ]


class TeamSnapshot(models.Model):
    id = models.BigAutoField(primary_key=True)
    occurrence = models.OneToOneField(
        SurveyOccurrence,
        on_delete=models.PROTECT,
        related_name="team_snapshot",
    )
    source_configuration = models.ForeignKey(
        TeamConfiguration,
        on_delete=models.PROTECT,
        related_name="snapshots",
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name="team_snapshots",
    )
    frozen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("id", "occurrence"),
                name="leai_team_snapshot_id_occurrence_uniq",
            ),
        ]


class TeamSnapshotItem(models.Model):
    id = models.BigAutoField(primary_key=True)
    snapshot = models.ForeignKey(
        TeamSnapshot,
        on_delete=models.PROTECT,
        related_name="items",
    )
    occurrence = models.ForeignKey(
        SurveyOccurrence,
        on_delete=models.PROTECT,
        related_name="team_snapshot_items",
    )
    item_number = models.PositiveIntegerField()
    stable_key = models.CharField(max_length=64)
    label = models.CharField(max_length=200)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("snapshot", "item_number"),
                name="leai_team_snapshot_item_number_uniq",
            ),
            models.UniqueConstraint(
                fields=("snapshot", "stable_key"),
                name="leai_team_snapshot_item_stable_key_uniq",
            ),
            models.UniqueConstraint(
                fields=("id", "occurrence"),
                name="leai_team_snapshot_item_id_occurrence_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(item_number__gte=1),
                name="leai_team_snapshot_item_number_positive",
            ),
        ]


class PdfImportBatch(models.Model):
    STATUSES = (
        ("prepared", "Prepared"),
        ("committed", "Committed"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    )

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    occurrence = models.ForeignKey(
        SurveyOccurrence,
        on_delete=models.PROTECT,
        related_name="pdf_import_batches",
    )
    committed_by = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="committed_pdf_import_batches",
    )
    idempotency_key_hash = models.CharField(max_length=64)
    manifest_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUSES, default="prepared")
    manifest = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("id", "occurrence"),
                name="leai_pdf_import_batch_id_occurrence_uniq",
            ),
            models.UniqueConstraint(
                fields=("occurrence", "idempotency_key_hash"),
                name="leai_pdf_import_batch_occurrence_idempotency_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(
                    status__in=[
                        "prepared",
                        "committed",
                        "processing",
                        "completed",
                        "failed",
                    ],
                ),
                name="leai_pdf_import_batch_status_valid",
            ),
            models.CheckConstraint(
                check=models.Q(idempotency_key_hash__regex=r"^[0-9a-f]{64}$"),
                name="leai_pdf_import_batch_idempotency_hash_valid",
            ),
            models.CheckConstraint(
                check=models.Q(manifest_digest__regex=r"^[0-9a-f]{64}$"),
                name="leai_pdf_import_batch_manifest_digest_valid",
            ),
        ]


class ResponseSession(models.Model):
    SOURCES = (("student", "Student"), ("pdf", "PDF"))
    STATUSES = (
        ("active", "Active"),
        ("completed", "Completed"),
        ("closed", "Closed"),
    )

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    occurrence = models.ForeignKey(
        SurveyOccurrence,
        on_delete=models.PROTECT,
        related_name="response_sessions",
    )
    pdf_import_batch = models.ForeignKey(
        PdfImportBatch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="response_sessions",
    )
    team_snapshot_item = models.ForeignKey(
        TeamSnapshotItem,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="response_sessions",
    )
    capability_nonce = models.CharField(max_length=128, null=True, blank=True)
    capability_digest = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
    )
    capability_key_version = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=16, choices=SOURCES)
    status = models.CharField(max_length=16, choices=STATUSES, default="active")
    research_consent = models.BooleanField(default=False)
    next_message_sequence = models.PositiveIntegerField(default=1)
    turn_version = models.PositiveIntegerField(default=1)
    flow_state = models.JSONField(default=dict)
    completed_at = models.DateTimeField(null=True, blank=True)
    completion_snapshot = models.JSONField(null=True, blank=True)
    certificate_code = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
    )
    certificate_issued_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(source__in=["student", "pdf"]),
                name="leai_response_session_source_valid",
            ),
            models.CheckConstraint(
                check=models.Q(status__in=["active", "completed", "closed"]),
                name="leai_response_session_status_valid",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(
                        source="student",
                        pdf_import_batch__isnull=True,
                        capability_nonce__isnull=False,
                        capability_digest__isnull=False,
                        capability_key_version__isnull=False,
                    )
                    | models.Q(
                        source="pdf",
                        pdf_import_batch__isnull=False,
                        capability_nonce__isnull=True,
                        capability_digest__isnull=True,
                        capability_key_version__isnull=True,
                    )
                ),
                name="leai_response_session_source_shape_valid",
            ),
            models.CheckConstraint(
                check=models.Q(capability_nonce__isnull=True)
                | ~models.Q(capability_nonce=""),
                name="leai_response_session_capability_nonce_nonempty",
            ),
            models.CheckConstraint(
                check=models.Q(capability_digest__isnull=True)
                | models.Q(capability_digest__regex=r"^[0-9a-f]{64}$"),
                name="leai_response_session_capability_digest_valid",
            ),
            models.CheckConstraint(
                check=models.Q(capability_key_version__isnull=True)
                | models.Q(capability_key_version__gte=1),
                name="leai_response_session_capability_key_positive",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(
                        completed_at__isnull=True,
                        completion_snapshot__isnull=True,
                    )
                    | models.Q(
                        completed_at__isnull=False,
                        completion_snapshot__isnull=False,
                    )
                ),
                name="leai_response_session_completion_shape_valid",
            ),
            models.CheckConstraint(
                check=models.Q(next_message_sequence__gte=1),
                name="leai_response_session_next_sequence_positive",
            ),
            models.CheckConstraint(
                check=models.Q(turn_version__gte=1),
                name="leai_response_session_turn_version_positive",
            ),
        ]


class ResponseMessage(models.Model):
    ROLES = (
        ("student", "Student"),
        ("assistant", "Assistant"),
        ("system", "System"),
    )
    INPUT_METHODS = (("typed", "Typed"), ("voice", "Voice"))

    id = models.BigAutoField(primary_key=True)
    response_session = models.ForeignKey(
        ResponseSession,
        on_delete=models.PROTECT,
        related_name="messages",
    )
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=16, choices=ROLES)
    input_method = models.CharField(
        max_length=16,
        choices=INPUT_METHODS,
        null=True,
        blank=True,
    )
    content = models.TextField()
    attribution = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("response_session", "sequence"),
                name="leai_response_message_sequence_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(sequence__gte=1),
                name="leai_response_message_sequence_positive",
            ),
            models.CheckConstraint(
                check=models.Q(role__in=["student", "assistant", "system"]),
                name="leai_response_message_role_valid",
            ),
            models.CheckConstraint(
                check=models.Q(input_method__isnull=True)
                | models.Q(role="student", input_method__in=["typed", "voice"]),
                name="leai_response_message_input_shape_valid",
            ),
        ]


class PdfImportJob(models.Model):
    STATUSES = (
        ("pending", "Pending"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    )

    id = models.BigAutoField(primary_key=True)
    batch = models.ForeignKey(
        PdfImportBatch,
        on_delete=models.PROTECT,
        related_name="jobs",
    )
    job_number = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=STATUSES, default="pending")
    result = models.JSONField(null=True, blank=True)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "job_number"),
                name="leai_pdf_import_job_number_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(job_number__gte=1),
                name="leai_pdf_import_job_number_positive",
            ),
            models.CheckConstraint(
                check=models.Q(status__in=["pending", "running", "completed", "failed"]),
                name="leai_pdf_import_job_status_valid",
            ),
        ]


class MutationReceipt(models.Model):
    MAX_RESULT_BYTES = MAX_MUTATION_RESULT_BYTES

    id = models.BigAutoField(primary_key=True)
    principal_scope = models.CharField(max_length=255)
    operation = models.CharField(max_length=128)
    target_key = models.CharField(max_length=255)
    idempotency_key_hash = models.CharField(max_length=64)
    request_hash = models.CharField(max_length=64)
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "principal_scope",
                    "operation",
                    "target_key",
                    "idempotency_key_hash",
                ),
                name="leai_mutation_receipt_scope_idempotency_uniq",
            ),
            models.CheckConstraint(
                check=~models.Q(principal_scope=""),
                name="leai_mutation_receipt_principal_nonempty",
            ),
            models.CheckConstraint(
                check=~models.Q(operation=""),
                name="leai_mutation_receipt_operation_nonempty",
            ),
            models.CheckConstraint(
                check=~models.Q(target_key=""),
                name="leai_mutation_receipt_target_nonempty",
            ),
            models.CheckConstraint(
                check=models.Q(idempotency_key_hash__regex=r"^[0-9a-f]{64}$"),
                name="leai_mutation_receipt_idempotency_hash_valid",
            ),
            models.CheckConstraint(
                check=models.Q(request_hash__regex=r"^[0-9a-f]{64}$"),
                name="leai_mutation_receipt_request_hash_valid",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(result__isnull=True, completed_at__isnull=True)
                    | models.Q(result__isnull=False, completed_at__isnull=False)
                ),
                name="leai_mutation_receipt_completion_shape_valid",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(result__isnull=True)
                    | Exact(
                        models.Func(
                            models.F("result"),
                            function="jsonb_typeof",
                            output_field=models.CharField(),
                        ),
                        models.Value("object"),
                    )
                ),
                name="leai_mutation_receipt_result_object",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(result__isnull=True)
                    | LessThanOrEqual(
                        models.Func(
                            models.F("result"),
                            function="leai_jsonb_canonical_size",
                            output_field=models.BigIntegerField(),
                        ),
                        models.Value(MAX_MUTATION_RESULT_BYTES),
                    )
                ),
                name="leai_mutation_receipt_result_bounded",
            ),
        ]
