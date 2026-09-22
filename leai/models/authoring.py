import uuid

from django.db import models

from .identity import Course, InstructorAccount


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
