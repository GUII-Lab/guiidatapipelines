from django.urls import path

from .environment import environment_required, environment_view
from .instructor_auth import instructor_csrf_view, instructor_me_view, instructor_password_view, instructor_sessions_view
from .instructor_courses import instructor_course_view, instructor_courses_view
from .research_debug import research_debug_settings_view, student_debug_access_view
from .response_search import response_search_view
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
    path("surveys/<uuid:survey_id>/", environment_required(student_survey_view), name="leai-student-survey"),
    path("surveys/<uuid:survey_id>/sessions/", environment_required(student_sessions_view), name="leai-student-sessions"),
    path("surveys/<uuid:survey_id>/sessions/<uuid:session_id>/", environment_required(student_session_view), name="leai-student-session"),
    path("surveys/<uuid:survey_id>/sessions/<uuid:session_id>/debug/", environment_required(student_session_debug_view), name="leai-student-session-debug"),
    path("surveys/<uuid:survey_id>/debug-access/", environment_required(student_debug_access_view), name="leai-student-debug-access"),
    path("surveys/<uuid:survey_id>/sessions/<uuid:session_id>/turns/", environment_required(student_turns_view), name="leai-student-turns"),
    path("surveys/<uuid:survey_id>/sessions/<uuid:session_id>/finalize/", environment_required(student_finalize_view), name="leai-student-finalize"),
]
