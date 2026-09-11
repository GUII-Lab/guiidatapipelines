from django.contrib import admin
from .models import *

# Register your models here.
admin.site.register(Message)
admin.site.register(User)
admin.site.register(CustomGPT)
admin.site.register(FireData)
admin.site.register(FeedbackMessage)
admin.site.register(FeedbackGPT)
admin.site.register(Image)


@admin.register(ResponseSession)
class ResponseSessionAdmin(admin.ModelAdmin):
    list_display = (
        'public_id',
        'course',
        'survey',
        'client_session_id',
        'source',
        'started_at',
        'completed_at',
    )
    list_filter = ('source', 'course', 'started_at', 'completed_at')
    search_fields = (
        '=public_id',
        'client_session_id',
        'course__course_id',
        'survey__name',
    )
    list_select_related = ('course', 'survey')

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ('slug', 'name', 'is_active', 'created_at', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('slug', 'name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(InstructorAccount)
class InstructorAccountAdmin(admin.ModelAdmin):
    list_display = (
        'email',
        'display_name',
        'auth_provider',
        'must_change_password',
        'is_active',
        'created_at',
    )
    list_filter = ('auth_provider', 'must_change_password', 'is_active')
    search_fields = ('email', 'display_name')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('user',)


@admin.register(InstitutionMembership)
class InstitutionMembershipAdmin(admin.ModelAdmin):
    list_display = (
        'institution',
        'instructor',
        'role',
        'is_active',
        'created_at',
    )
    list_filter = ('role', 'is_active', 'institution')
    search_fields = ('institution__slug', 'institution__name', 'instructor__email')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('institution', 'instructor', 'instructor__user')


@admin.register(InstructorSession)
class InstructorSessionAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'instructor_email',
        'created_at',
        'expires_at',
        'revoked_at',
        'last_used_at',
    )
    list_filter = ('created_at', 'expires_at', 'revoked_at')
    search_fields = ('instructor__email',)
    list_select_related = ('instructor', 'instructor__user')
    exclude = ('token_digest',)

    @admin.display(description='Instructor email', ordering='instructor__email')
    def instructor_email(self, obj):
        return obj.instructor.email

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CourseMembership)
class CourseMembershipAdmin(admin.ModelAdmin):
    list_display = (
        'course',
        'instructor_email',
        'role',
        'can_publish',
        'can_export',
        'is_active',
    )
    list_filter = ('role', 'can_publish', 'can_export', 'is_active')
    search_fields = (
        'course__course_id',
        'course__course_name',
        'institution_membership__instructor__email',
    )
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = (
        'course',
        'institution_membership',
        'institution_membership__instructor',
    )

    @admin.display(
        description='Instructor email',
        ordering='institution_membership__instructor__email',
    )
    def instructor_email(self, obj):
        return obj.institution_membership.instructor.email


@admin.register(InstructorAuditEvent)
class InstructorAuditEventAdmin(admin.ModelAdmin):
    list_display = (
        'event_id',
        'occurred_at',
        'actor',
        'action',
        'outcome',
        'course_id_snapshot',
        'target_type',
        'target_id',
    )
    list_filter = ('action', 'outcome', 'course', 'occurred_at')
    search_fields = (
        'actor__email',
        'course_id_snapshot',
        'target_id',
        '=event_id',
    )
    ordering = ('-occurred_at', '-id')
    list_select_related = ('actor', 'actor__user', 'session', 'course')

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FormSchema)
class FormSchemaAdmin(admin.ModelAdmin):
    list_display = ('schema_id', 'title', 'course_label', 'week_number', 'version', 'is_active', 'updated_at')
    list_filter = ('is_active', 'course_label', 'week_number')
    search_fields = ('schema_id', 'title', 'course_label')
    readonly_fields = ('created_at', 'updated_at')


class ImmutableQuestionSetAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(QuestionSet)
class QuestionSetAdmin(ImmutableQuestionSetAdmin):
    list_display = ('public_id', 'title', 'course', 'owner', 'template_id', 'updated_at')
    list_filter = ('audience', 'course', 'archived_at')
    search_fields = ('=public_id', 'title', 'course__course_id', 'owner__email')
    list_select_related = ('course', 'owner')


@admin.register(QuestionSetDraft)
class QuestionSetDraftAdmin(ImmutableQuestionSetAdmin):
    list_display = ('public_id', 'question_set', 'version', 'updated_by', 'updated_at')
    search_fields = ('=public_id', 'question_set__title', 'question_set__course__course_id')
    list_select_related = ('question_set', 'question_set__course', 'updated_by')


@admin.register(QuestionSetRevision)
class QuestionSetRevisionAdmin(ImmutableQuestionSetAdmin):
    list_display = ('public_id', 'question_set', 'revision_number', 'created_by', 'created_at')
    search_fields = ('=public_id', 'question_set__title', 'content_hash')
    list_select_related = ('question_set', 'created_by')


@admin.register(PreviewSession)
class PreviewSessionAdmin(ImmutableQuestionSetAdmin):
    list_display = ('public_id', 'revision', 'instructor', 'created_at', 'expires_at', 'completed_at')
    list_filter = ('created_at', 'expires_at', 'completed_at')
    search_fields = ('=public_id', 'instructor__email', 'revision__question_set__title')
    exclude = ('token_digest',)
    list_select_related = ('revision', 'revision__question_set', 'instructor')


@admin.register(QuestionSetSurvey)
class QuestionSetSurveyAdmin(ImmutableQuestionSetAdmin):
    list_display = ('survey', 'revision', 'created_by', 'created_at')
    search_fields = ('survey__public_id', 'survey__name', 'revision__question_set__title')
    list_select_related = ('survey', 'revision', 'revision__question_set', 'created_by')


class TeamInline(admin.TabularInline):
    model = Team
    extra = 0


@admin.register(TeamConfiguration)
class TeamConfigurationAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'course', 'label_prefix', 'color', 'archived', 'updated_at')
    list_filter = ('archived', 'color', 'course')
    search_fields = ('name', 'course__course_id')
    inlines = [TeamInline]


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('id', 'team_configuration', 'number', 'size', 'display_name')
    list_filter = ('team_configuration',)
    search_fields = ('display_name',)


class SurveyTeamInline(admin.TabularInline):
    model = SurveyTeam
    extra = 0


@admin.register(SurveyTeamSnapshot)
class SurveyTeamSnapshotAdmin(admin.ModelAdmin):
    list_display = ('id', 'survey', 'name', 'label_prefix', 'color', 'source_configuration', 'created_at')
    list_filter = ('color',)
    search_fields = ('name', 'survey__name', 'survey__public_id')
    inlines = [SurveyTeamInline]


@admin.register(SurveyTeam)
class SurveyTeamAdmin(admin.ModelAdmin):
    list_display = ('id', 'snapshot', 'number', 'size', 'display_name')
    list_filter = ('snapshot',)
    search_fields = ('display_name',)


@admin.register(SessionTeamAssignment)
class SessionTeamAssignmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'session_id', 'survey_team', 'assigned_at')
    search_fields = ('session_id',)
