from django.db import models

from .authoring import SurveyOccurrence
from .identity import Course, InstructorAccount
from .responses import ResponseMessage, ResponseSession


ANALYSIS_ORIGIN = (
    ("feedback_chat", "Feedback Chat"),
    ("instructor_insights", "Instructor Insights"),
    ("analyzer", "Analyzer"),
)


class AnalysisSnapshot(models.Model):
    id = models.BigAutoField(primary_key=True)
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="analysis_snapshots")
    scope_key = models.CharField(max_length=255)
    algorithm_version = models.CharField(max_length=64)
    response_cutoff_version = models.PositiveBigIntegerField()
    model_policy_version = models.CharField(max_length=64)
    prompt_policy_version = models.CharField(max_length=64)
    source_count = models.PositiveIntegerField()
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("course", "scope_key", "algorithm_version", "response_cutoff_version", "model_policy_version", "prompt_policy_version"),
                name="leai_analysis_snapshot_generation_uniq",
            ),
            models.CheckConstraint(check=~models.Q(scope_key=""), name="leai_analysis_snapshot_scope_nonempty"),
        ]


class AnalysisChatSession(models.Model):
    id = models.BigAutoField(primary_key=True)
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="analysis_chat_sessions")
    actor_account = models.ForeignKey(InstructorAccount, on_delete=models.PROTECT, related_name="analysis_chat_sessions")
    origin_surface = models.CharField(max_length=32, choices=ANALYSIS_ORIGIN)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(origin_surface__in=["feedback_chat", "instructor_insights", "analyzer"]),
                name="leai_analysis_chat_origin_valid",
            ),
        ]


class AnalysisScopeOccurrence(models.Model):
    id = models.BigAutoField(primary_key=True)
    analysis_chat_session = models.ForeignKey(AnalysisChatSession, on_delete=models.PROTECT, related_name="occurrence_scope")
    survey_occurrence = models.ForeignKey(SurveyOccurrence, on_delete=models.PROTECT, related_name="analysis_chat_scopes")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("analysis_chat_session", "survey_occurrence"),
                name="leai_analysis_scope_occurrence_uniq",
            ),
        ]


class AnalysisChatMessage(models.Model):
    ROLES = (("user", "User"), ("assistant", "Assistant"), ("system", "System"))
    INPUT_METHODS = (("typed", "Typed"), ("voice", "Voice"))

    id = models.BigAutoField(primary_key=True)
    analysis_chat_session = models.ForeignKey(AnalysisChatSession, on_delete=models.PROTECT, related_name="messages")
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=16, choices=ROLES)
    input_method = models.CharField(max_length=16, choices=INPUT_METHODS, null=True, blank=True)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("analysis_chat_session", "sequence"), name="leai_analysis_chat_message_sequence_uniq"),
            models.CheckConstraint(check=models.Q(sequence__gte=1), name="leai_analysis_chat_message_sequence_positive"),
            models.CheckConstraint(check=models.Q(role__in=["user", "assistant", "system"]), name="leai_analysis_chat_message_role_valid"),
            models.CheckConstraint(
                check=models.Q(role="user", input_method__isnull=False, input_method__in=["typed", "voice"])
                | models.Q(role__in=["assistant", "system"], input_method__isnull=True),
                name="leai_analysis_chat_message_input_shape_valid",
            ),
        ]


class AnalysisCitation(models.Model):
    id = models.BigAutoField(primary_key=True)
    analysis_chat_message = models.ForeignKey(AnalysisChatMessage, null=True, blank=True, on_delete=models.PROTECT, related_name="citations")
    analysis_snapshot = models.ForeignKey(AnalysisSnapshot, null=True, blank=True, on_delete=models.PROTECT, related_name="citations")
    response_session = models.ForeignKey(ResponseSession, null=True, blank=True, on_delete=models.PROTECT, related_name="analysis_citations")
    response_message = models.ForeignKey(ResponseMessage, null=True, blank=True, on_delete=models.PROTECT, related_name="analysis_citations")
    claim_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(models.Q(analysis_chat_message__isnull=False, analysis_snapshot__isnull=True)
                       | models.Q(analysis_chat_message__isnull=True, analysis_snapshot__isnull=False)),
                name="leai_analysis_citation_owner_xor",
            ),
            models.CheckConstraint(
                check=(models.Q(response_session__isnull=False, response_message__isnull=True)
                       | models.Q(response_session__isnull=True, response_message__isnull=False)),
                name="leai_analysis_citation_source_xor",
            ),
            models.CheckConstraint(check=~models.Q(claim_key=""), name="leai_analysis_citation_claim_nonempty"),
        ]
