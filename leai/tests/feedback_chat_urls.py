from django.urls import include, path

from leai.api.feedback_chat import (
    feedback_chat_detail_view,
    feedback_chat_job_view,
    feedback_chat_occurrences_view,
    feedback_chat_scope_view,
    feedback_chat_turn_view,
    feedback_chats_view,
)

urlpatterns = [
    path("datapipeline/api/v1/", include("leai.api.urls")),
    path("datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/occurrences/", feedback_chat_occurrences_view),
    path("datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/chats/", feedback_chats_view),
    path("datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/chats/<uuid:chat_id>/", feedback_chat_detail_view),
    path("datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/chats/<uuid:chat_id>/scope/", feedback_chat_scope_view),
    path("datapipeline/api/v1/instructor_courses/<uuid:course_id>/analysis/chats/<uuid:chat_id>/turns/", feedback_chat_turn_view),
    path("datapipeline/api/v1/instructor_courses/<uuid:course_id>/jobs/<uuid:job_id>/", feedback_chat_job_view),
]
