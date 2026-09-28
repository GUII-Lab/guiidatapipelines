from django.urls import path

from .environment import environment_required, environment_view
from .instructor_auth import instructor_csrf_view, instructor_me_view, instructor_password_view, instructor_sessions_view
from .instructor_courses import instructor_course_view, instructor_courses_view
from .research_debug import research_debug_settings_view, student_debug_access_view
from .response_search import response_search_view
from .feedback_analyzer import (
    analysis_ngrams_view,
    analysis_overview_view,
    analysis_progress_view,
    analysis_response_detail_view,
    analysis_responses_view,
    analysis_settings_view,
    analysis_certificate_verify_view,
    matching_signals_view,
)
from .feedback_chat import (
    feedback_chat_detail_view,
    feedback_chat_job_view,
    feedback_chat_occurrences_view,
    feedback_chat_scope_view,
    feedback_chat_turn_view,
    feedback_chats_view,
)
from .student_responses import (
    student_survey_view, student_sessions_view, student_session_view, student_session_debug_view, student_turns_view,
    student_finalize_view,
)


urlpatterns = [
    path("environment/", environment_view, name="leai-environment"),
    path("instructor_csrf/", environment_required(instructor_csrf_view), name="leai-instructor-csrf"),
    path("instructor_sessions/", environment_required(instructor_sessions_view), name="leai-instructor-sessions"),
    path("instructor_me/", environment_required(instructor_me_view), name="leai-instructor-me"),
    path("instructor_password/", environment_required(instructor_password_view), name="leai-instructor-password"),
    path("instructor_courses/", environment_required(instructor_courses_view), name="leai-instructor-courses"),
    path("instructor_courses/<uuid:course_id>/", environment_required(instructor_course_view), name="leai-instructor-course"),
    path("instructor_courses/<uuid:course_id>/debug-settings/", environment_required(research_debug_settings_view), name="leai-research-debug-settings"),
    path("instructor_courses/<uuid:course_id>/responses/search/", environment_required(response_search_view), name="leai-response-search"),
    path("instructor_courses/<uuid:course_id>/analysis/occurrences/", environment_required(feedback_chat_occurrences_view), name="leai-feedback-chat-occurrences"),
    path("instructor_courses/<uuid:course_id>/analysis/chats/", environment_required(feedback_chats_view), name="leai-feedback-chats"),
    path("instructor_courses/<uuid:course_id>/analysis/chats/<uuid:chat_id>/", environment_required(feedback_chat_detail_view), name="leai-feedback-chat-detail"),
    path("instructor_courses/<uuid:course_id>/analysis/chats/<uuid:chat_id>/scope/", environment_required(feedback_chat_scope_view), name="leai-feedback-chat-scope"),
    path("instructor_courses/<uuid:course_id>/analysis/chats/<uuid:chat_id>/turns/", environment_required(feedback_chat_turn_view), name="leai-feedback-chat-turns"),
    path("instructor_courses/<uuid:course_id>/jobs/<uuid:job_id>/", environment_required(feedback_chat_job_view), name="leai-feedback-chat-job"),
    path("instructor_courses/<uuid:course_id>/analysis/overview/", environment_required(analysis_overview_view), name="leai-analysis-overview"),
    path("instructor_courses/<uuid:course_id>/analysis/ngrams/", environment_required(analysis_ngrams_view), name="leai-analysis-ngrams"),
    path("instructor_courses/<uuid:course_id>/analysis/responses/", environment_required(analysis_responses_view), name="leai-analysis-responses"),
    path("instructor_courses/<uuid:course_id>/analysis/responses/<uuid:response_id>/", environment_required(analysis_response_detail_view), name="leai-analysis-response-detail"),
    path("instructor_courses/<uuid:course_id>/analysis/progress/", environment_required(analysis_progress_view), name="leai-analysis-progress"),
    path("instructor_courses/<uuid:course_id>/analysis/certificates/verify/", environment_required(analysis_certificate_verify_view), name="leai-analysis-certificate-verify"),
    path("instructor_courses/<uuid:course_id>/analysis-settings/", environment_required(analysis_settings_view), name="leai-analysis-settings"),
    path("surveys/<uuid:survey_id>/sessions/<uuid:session_id>/matching-signals/", environment_required(matching_signals_view), name="leai-matching-signals"),
    path("surveys/<uuid:survey_id>/", environment_required(student_survey_view), name="leai-student-survey"),
    path("surveys/<uuid:survey_id>/sessions/", environment_required(student_sessions_view), name="leai-student-sessions"),
    path("surveys/<uuid:survey_id>/sessions/<uuid:session_id>/", environment_required(student_session_view), name="leai-student-session"),
    path("surveys/<uuid:survey_id>/sessions/<uuid:session_id>/debug/", environment_required(student_session_debug_view), name="leai-student-session-debug"),
    path("surveys/<uuid:survey_id>/debug-access/", environment_required(student_debug_access_view), name="leai-student-debug-access"),
    path("surveys/<uuid:survey_id>/sessions/<uuid:session_id>/turns/", environment_required(student_turns_view), name="leai-student-turns"),
    path("surveys/<uuid:survey_id>/sessions/<uuid:session_id>/finalize/", environment_required(student_finalize_view), name="leai-student-finalize"),
]
