from django.urls import path

from leai.api.feedback_analyzer import (
    analysis_ngrams_view,
    analysis_overview_view,
    analysis_progress_view,
    analysis_response_detail_view,
    analysis_responses_view,
    analysis_settings_view,
    matching_signals_view,
)
from leai.api.course_settings import course_banner_settings_view
from leai.api.instructor_auth import instructor_csrf_view, instructor_sessions_view
from leai.api.student_responses import student_survey_view


urlpatterns = [
    path("datapipeline/api/v1/instructor_courses/<uuid:course_id>/banner-settings/", course_banner_settings_view),
    path("datapipeline/api/v1/instructor_csrf/", instructor_csrf_view),
    path("datapipeline/api/v1/instructor_sessions/", instructor_sessions_view),
    path(
        "datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/overview/",
        analysis_overview_view,
    ),
    path(
        "datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/ngrams/",
        analysis_ngrams_view,
    ),
    path(
        "datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/responses/",
        analysis_responses_view,
    ),
    path(
        "datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/responses/<uuid:response_id>/",
        analysis_response_detail_view,
    ),
    path(
        "datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/progress/",
        analysis_progress_view,
    ),
    path(
        "datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis-settings/",
        analysis_settings_view,
    ),
    path(
        "datapipeline/api/v1/surveys/<uuid:survey_id>/sessions/<uuid:session_id>/matching-signals/",
        matching_signals_view,
    ),
    path("datapipeline/api/v1/surveys/<uuid:survey_id>/", student_survey_view),
]
