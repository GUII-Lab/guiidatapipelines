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


@admin.register(FormSchema)
class FormSchemaAdmin(admin.ModelAdmin):
    list_display = ('schema_id', 'title', 'course_label', 'week_number', 'version', 'is_active', 'updated_at')
    list_filter = ('is_active', 'course_label', 'week_number')
    search_fields = ('schema_id', 'title', 'course_label')
    readonly_fields = ('created_at', 'updated_at')


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
    list_display = ('id', 'team_configuration', 'number', 'size')
    list_filter = ('team_configuration',)


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
    list_display = ('id', 'snapshot', 'number', 'size')
    list_filter = ('snapshot',)


@admin.register(SessionTeamAssignment)
class SessionTeamAssignmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'session_id', 'survey_team', 'assigned_at')
    search_fields = ('session_id',)