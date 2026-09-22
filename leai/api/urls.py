from django.urls import path

from .environment import environment_required, environment_view
from .instructor_auth import instructor_me_view, instructor_sessions_view
from .instructor_courses import instructor_course_view, instructor_courses_view
from .response_search import response_search_view


urlpatterns = [
    path("environment/", environment_view, name="leai-environment"),
    path("instructor_sessions/", environment_required(instructor_sessions_view), name="leai-instructor-sessions"),
    path("instructor_me/", environment_required(instructor_me_view), name="leai-instructor-me"),
    path("instructor_courses/", environment_required(instructor_courses_view), name="leai-instructor-courses"),
    path("instructor_courses/<uuid:course_id>/", environment_required(instructor_course_view), name="leai-instructor-course"),
    path("instructor_courses/<uuid:course_id>/responses/search/", environment_required(response_search_view), name="leai-response-search"),
]
