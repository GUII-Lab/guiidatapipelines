from django.urls import path

from . import views
from .views import *

urlpatterns = [
    path('api/message/', message_create, name='message_create'),
    path('api/create_new_gpt/', create_new_gpt, name='create_new_gpt'),
    path('api/list_custom_gpts/', list_custom_gpts, name='list_custom_gpts'),
    path('api/sendFireData/', sendFireData, name='sendFireData'),
    path('api/getOAI/', getOAI, name='getOAI'),
    path('api/list_feedback_gpts/', list_feedback_gpts, name='list_feedback_gpts'),
    path('api/feedback_message_api/', feedback_message_api, name='feedback_message_api'),
    path('api/feedback_messages_bulk_api/', feedback_messages_bulk_api, name='feedback_messages_bulk_api'),
    path('api/feedbackList/', feedbackList, name='feedbackList'),
    path('api/scList/', scList, name='scList'),
    path('api/messages/', get_messages_by_gpt, name='get_messages_by_gpt'),
    path('api/letsmessages/', get_lets_by_gpt, name='get_lets_by_gpt'),
    path('api/upload-image/', upload_image, name='upload_image'),
    path('api/image/<int:image_id>/', get_image, name='get_image'),
    path('api/images/', list_images, name='list_images'),
    path('api/openai-chat/', openai_chat, name='openai_chat'),
    path('api/openai-structured/', openai_structured, name='openai_structured'),
    path('api/openai-tts/', openai_tts, name='openai_tts'),
    path('api/openai-stt/', openai_stt, name='openai_stt'),
    # LEAI course management
    path('api/create_course/', create_course, name='create_course'),
    path('api/verify_course_password/', verify_course_password, name='verify_course_password'),
    path('api/create_feedback_gpt/', create_feedback_gpt, name='create_feedback_gpt'),
    path('api/feedback_gpts_by_course/', feedback_gpts_by_course, name='feedback_gpts_by_course'),
    path('api/get_feedback_gpt_by_public_id/', get_feedback_gpt_by_public_id, name='get_feedback_gpt_by_public_id'),
    # Form-mode schema registry (read-only for now; edits via Django admin)
    path('api/form_schemas/', list_form_schemas, name='list_form_schemas'),
    path('api/form_schemas/<str:schema_id>/', get_form_schema, name='get_form_schema'),
    path('api/feedback_messages_by_gpt/', feedback_messages_by_gpt, name='feedback_messages_by_gpt'),
    path('api/feedback_messages_by_course/', feedback_messages_by_course, name='feedback_messages_by_course'),
    # LEAI survey lifecycle & management
    path('api/set_survey_status/', set_survey_status, name='set_survey_status'),
    path('api/update_survey/', update_survey, name='update_survey'),
    path('api/clone_survey/', clone_survey, name='clone_survey'),
    path('api/delete_survey/', delete_survey, name='delete_survey'),
    path('api/export_survey_responses/', export_survey_responses, name='export_survey_responses'),
    # LEAI chat sessions & quick take
    path('api/leai_chat_sessions/', views.leai_chat_sessions_list, name='leai_chat_sessions_list'),
    path('api/leai_chat_sessions/<uuid:session_id>/', views.leai_chat_session_detail, name='leai_chat_session_detail'),
    path('api/leai_chat_sessions/<uuid:session_id>/turn/', views.leai_chat_session_turn, name='leai_chat_session_turn'),
    path('api/leai_chat_sessions/<uuid:session_id>/messages/<int:message_id>/', views.leai_chat_message_detail, name='leai_chat_message_detail'),
    path('api/leai_quicktake/generate/', views.leai_quicktake_generate, name='leai_quicktake_generate'),
    path('api/leai_quicktake/', views.leai_quicktake_fetch_or_delete, name='leai_quicktake'),
    # In-Group feedback: team configurations + survey snapshots + session assignments
    path('api/team_configurations/', list_team_configurations, name='list_team_configurations'),
    path('api/team_configurations/create/', create_team_configuration, name='create_team_configuration'),
    path('api/team_configurations/update/', update_team_configuration, name='update_team_configuration'),
    path('api/team_configurations/archive/', archive_team_configuration, name='archive_team_configuration'),
    path('api/team_configurations/delete/', delete_team_configuration, name='delete_team_configuration'),
    path('api/survey_team_snapshot/', get_survey_team_snapshot, name='get_survey_team_snapshot'),
    path('api/session_team_assignment/', assign_session_to_team, name='assign_session_to_team'),
    path('api/survey_team_assignments/', list_survey_team_assignments, name='list_survey_team_assignments'),
]
