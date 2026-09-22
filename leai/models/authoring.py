import uuid

from django.core.exceptions import ValidationError
from django.db import models

from .identity import (
    Course, CourseMembership, Institution,
    InstitutionMembership, InstructorAccount,
)


class QuestionSet(models.Model):
    AUDIENCE = (("individual", "Individual"), ("team", "Team"))
    COLLECTION_STYLE = (("guided", "Guided"), ("open", "Open"))

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name="question_sets",
    )
    owner = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="owned_question_sets",
    )
    source_question_set_revision = models.ForeignKey(
        "QuestionSetRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sourced_question_sets",
    )
    title = models.CharField(max_length=200)
    audience = models.CharField(max_length=16, choices=AUDIENCE, default="individual")
    collection_style = models.CharField(
        max_length=16,
        choices=COLLECTION_STYLE,
        default="guided",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(audience__in=["individual", "team"]),
                name="leai_question_set_audience_valid",
            ),
            models.CheckConstraint(
                check=models.Q(collection_style__in=["guided", "open"]),
                name="leai_question_set_collection_style_valid",
            ),
            models.CheckConstraint(
                check=~models.Q(audience="team", collection_style="open"),
                name="leai_question_set_team_open_rejected",
            ),
        ]


class QuestionSetDraft(models.Model):
    id = models.BigAutoField(primary_key=True)
    question_set = models.OneToOneField(
        QuestionSet,
        on_delete=models.PROTECT,
        related_name="draft",
    )
    current_version = models.PositiveIntegerField(default=1)
    base_revision = models.ForeignKey(
        "QuestionSetRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="based_drafts",
    )
    canonical_body = models.JSONField(default=dict)
    updated_by = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="updated_question_set_drafts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(current_version__gte=1),
                name="leai_question_set_draft_current_version_positive",
            ),
        ]


class QuestionSetDraftVersion(models.Model):
    CHANGE_KIND = (
        ("manual", "Manual"),
        ("ai", "AI"),
        ("restore", "Restore"),
        ("delete", "Delete"),
    )

    id = models.BigAutoField(primary_key=True)
    draft = models.ForeignKey(
        QuestionSetDraft,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=64)
    canonical_body = models.JSONField(default=dict)
    change_kind = models.CharField(max_length=16, choices=CHANGE_KIND, default="manual")
    created_by = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="created_question_set_draft_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("draft", "version_number"),
                name="leai_question_set_draft_version_number_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(change_kind__in=["manual", "ai", "restore", "delete"]),
                name="leai_question_set_draft_version_change_kind_valid",
            ),
            models.CheckConstraint(
                check=models.Q(content_hash__regex=r"^[0-9a-f]{64}$"),
                name="leai_question_set_draft_version_hash_valid",
            ),
        ]


class QuestionSetRevision(models.Model):
    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    question_set = models.ForeignKey(
        QuestionSet,
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField()
    source_draft_version = models.ForeignKey(
        QuestionSetDraftVersion,
        on_delete=models.PROTECT,
        related_name="compiled_revisions",
    )
    content_hash = models.CharField(max_length=64)
    compiled_protocol = models.JSONField(default=dict)
    compiler_version = models.CharField(max_length=64)
    engine_version = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="created_question_set_revisions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("question_set", "revision_number"),
                name="leai_question_set_revision_number_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(content_hash__regex=r"^[0-9a-f]{64}$"),
                name="leai_question_set_revision_hash_valid",
            ),
        ]


class AuthoringConversation(models.Model):
    ORIGIN_SURFACES = (
        ("builder", "Builder"),
        ("instructor_insights", "Instructor Insights"),
        ("survey_list", "Survey List"),
        ("template", "Template"),
    )

    id = models.BigAutoField(primary_key=True)
    question_set = models.OneToOneField(
        QuestionSet,
        on_delete=models.PROTECT,
        related_name="authoring_conversation",
    )
    origin_surface = models.CharField(max_length=32, choices=ORIGIN_SURFACES)
    created_by = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="created_authoring_conversations",
    )
    source_analysis_snapshot = models.ForeignKey(
        "leai.AnalysisSnapshot",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="seeded_authoring_conversations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        if not self.source_analysis_snapshot_id:
            return
        course_id = self.question_set.course_id
        if self.source_analysis_snapshot.course_id != course_id:
            raise ValidationError({"source_analysis_snapshot": "Source snapshot must belong to the Question Set course."})
        actor = self.created_by
        course = self.question_set.course
        if not actor.is_active or course.lifecycle_state != "active":
            raise ValidationError({"created_by": "Actor does not have active course access."})
        if actor.platform_role == "platform_admin":
            return
        direct = CourseMembership.objects.filter(
            course_id=course_id,
            institution_membership__account_id=actor.pk,
            institution_membership__institution_id=course.institution_id,
            institution_membership__is_active=True,
            role__in=["owner", "instructor", "ta"],
        ).exists()
        researcher = InstitutionMembership.objects.filter(
            account_id=actor.pk,
            institution_id=course.institution_id,
            is_active=True,
            role="researcher",
        ).exclude(
            course_access_restrictions__course_id=course_id,
            course_access_restrictions__denied=True,
        ).exists()
        if not direct and not researcher:
            raise ValidationError({"created_by": "Actor does not have course authoring and analysis access."})

    def save(self, *args, **kwargs):
        if self.source_analysis_snapshot_id:
            self.full_clean()
        return super().save(*args, **kwargs)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    origin_surface__in=[
                        "builder",
                        "instructor_insights",
                        "survey_list",
                        "template",
                    ],
                ),
                name="leai_authoring_conversation_origin_valid",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(origin_surface="instructor_insights", source_analysis_snapshot__isnull=False)
                    | (~models.Q(origin_surface="instructor_insights") & models.Q(source_analysis_snapshot__isnull=True))
                ),
                name="leai_authoring_conversation_source_shape_valid",
            ),
        ]


class AuthoringMessage(models.Model):
    ROLES = (("user", "User"), ("assistant", "Assistant"), ("system", "System"))
    INPUT_METHODS = (("typed", "Typed"), ("voice", "Voice"))

    id = models.BigAutoField(primary_key=True)
    conversation = models.ForeignKey(
        AuthoringConversation,
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("conversation", "sequence"),
                name="leai_authoring_message_sequence_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(role__in=["user", "assistant", "system"]),
                name="leai_authoring_message_role_valid",
            ),
            models.CheckConstraint(
                check=models.Q(
                    models.Q(
                        role="user",
                        input_method__isnull=False,
                        input_method__in=["typed", "voice"],
                    ),
                    models.Q(role__in=["assistant", "system"], input_method__isnull=True),
                    _connector=models.Q.OR,
                ),
                name="leai_authoring_message_input_shape_valid",
            ),
        ]


class AuthoringRun(models.Model):
    STATUSES = (
        ("pending", "Pending"),
        ("running", "Running"),
        ("succeeded", "Succeeded"),
        ("failed", "Failed"),
        ("canceled", "Canceled"),
    )

    id = models.BigAutoField(primary_key=True)
    conversation = models.ForeignKey(
        AuthoringConversation,
        on_delete=models.PROTECT,
        related_name="runs",
    )
    base_draft_version = models.ForeignKey(
        QuestionSetDraftVersion,
        on_delete=models.PROTECT,
        related_name="authoring_runs",
    )
    requested_by = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="requested_authoring_runs",
    )
    status = models.CharField(max_length=16, choices=STATUSES, default="pending")
    source_provenance_snapshot = models.JSONField()
    bounded_result = models.JSONField(null=True, blank=True)
    bounded_error_code = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    status__in=["pending", "running", "succeeded", "failed", "canceled"],
                ),
                name="leai_authoring_run_status_valid",
            ),
            models.CheckConstraint(
                check=models.Q(source_provenance_snapshot__isnull=False),
                name="leai_authoring_run_provenance_present",
            ),
        ]


class QuestionSetTemplate(models.Model):
    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(max_length=200)
    owner_account = models.ForeignKey(
        InstructorAccount,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="owned_question_set_templates",
    )
    owner_institution = models.ForeignKey(
        Institution,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="owned_question_set_templates",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    models.Q(owner_account__isnull=False, owner_institution__isnull=True),
                    models.Q(owner_account__isnull=True, owner_institution__isnull=False),
                    _connector=models.Q.OR,
                ),
                name="leai_question_set_template_owner_scope_xor",
            ),
        ]


class QuestionSetTemplateRevision(models.Model):
    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    template = models.ForeignKey(
        QuestionSetTemplate,
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField()
    source_question_set_revision = models.ForeignKey(
        QuestionSetRevision,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sourced_template_revisions",
    )
    content_hash = models.CharField(max_length=64)
    canonical_body = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="created_question_set_template_revisions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("template", "revision_number"),
                name="leai_template_revision_number_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(content_hash__regex=r"^[0-9a-f]{64}$"),
                name="leai_template_revision_hash_valid",
            ),
        ]


class PreviewSession(models.Model):
    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    revision = models.ForeignKey(
        QuestionSetRevision,
        on_delete=models.PROTECT,
        related_name="preview_sessions",
    )
    actor = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="preview_sessions",
    )
    capability_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(capability_digest__regex=r"^[0-9a-f]{64}$"),
                name="leai_preview_session_capability_digest_valid",
            ),
        ]


class PreviewMessage(models.Model):
    ROLES = (("student", "Student"), ("assistant", "Assistant"), ("system", "System"))

    id = models.BigAutoField(primary_key=True)
    preview_session = models.ForeignKey(
        PreviewSession,
        on_delete=models.PROTECT,
        related_name="messages",
    )
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=16, choices=ROLES)
    content = models.TextField()
    attribution = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("preview_session", "sequence"),
                name="leai_preview_message_sequence_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(role__in=["student", "assistant", "system"]),
                name="leai_preview_message_role_valid",
            ),
        ]


class PreviewDecision(models.Model):
    DECISIONS = (("completed", "Completed"), ("skipped", "Skipped"))

    id = models.BigAutoField(primary_key=True)
    revision = models.OneToOneField(
        QuestionSetRevision,
        on_delete=models.PROTECT,
        related_name="preview_decision",
    )
    actor = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="preview_decisions",
    )
    decision = models.CharField(max_length=16, choices=DECISIONS)
    decided_at = models.DateTimeField(auto_now_add=True)
    idempotency_key_hash = models.CharField(max_length=64, unique=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(decision__in=["completed", "skipped"]),
                name="leai_preview_decision_valid",
            ),
            models.CheckConstraint(
                check=models.Q(idempotency_key_hash__regex=r"^[0-9a-f]{64}$"),
                name="leai_preview_decision_idempotency_hash_valid",
            ),
        ]


class SurveyOccurrence(models.Model):
    PROVENANCE = (("native", "Native"), ("imported", "Imported"))
    MANAGEMENT_MODES = (
        ("managed", "Managed"),
        ("imported_read_only", "Imported read only"),
    )

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    revision = models.ForeignKey(
        QuestionSetRevision,
        on_delete=models.PROTECT,
        related_name="survey_occurrences",
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name="survey_occurrences",
    )
    created_by = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="created_survey_occurrences",
    )
    label = models.CharField(max_length=200)
    provenance = models.CharField(max_length=16, choices=PROVENANCE, default="native")
    management_mode = models.CharField(
        max_length=32,
        choices=MANAGEMENT_MODES,
        default="managed",
    )
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    manually_closed_at = models.DateTimeField(null=True, blank=True)
    settings_version = models.PositiveIntegerField(default=1)
    completion_certificate_enabled = models.BooleanField(default=False)
    completed_response_download_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(provenance__in=["native", "imported"]),
                name="leai_survey_occurrence_provenance_valid",
            ),
            models.CheckConstraint(
                check=models.Q(management_mode__in=["managed", "imported_read_only"]),
                name="leai_survey_occurrence_management_mode_valid",
            ),
            models.CheckConstraint(
                check=models.Q(settings_version__gte=1),
                name="leai_survey_occurrence_settings_version_positive",
            ),
            models.CheckConstraint(
                check=models.Q(opens_at__isnull=True)
                | models.Q(closes_at__isnull=True)
                | models.Q(opens_at__lte=models.F("closes_at")),
                name="leai_survey_occurrence_schedule_ordered",
            ),
        ]
