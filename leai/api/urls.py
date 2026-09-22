from django.urls import path

from .environment import environment_view
from .instructor_auth import instructor_me_view, instructor_sessions_view


urlpatterns = [
    path("environment/", environment_view, name="leai-environment"),
    path("instructor_sessions/", instructor_sessions_view, name="leai-instructor-sessions"),
    path("instructor_me/", instructor_me_view, name="leai-instructor-me"),
]
