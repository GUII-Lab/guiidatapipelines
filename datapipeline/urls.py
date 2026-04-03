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
    path('api/feedbackList/', feedbackList, name='feedbackList'),
    path('api/scList/', scList, name='scList'),
    path('api/messages/', get_messages_by_gpt, name='get_messages_by_gpt'),
    path('api/letsmessages/', get_lets_by_gpt, name='get_lets_by_gpt'),
    path('api/upload-image/', upload_image, name='upload_image'),
    path('api/image/<int:image_id>/', get_image, name='get_image'),
    path('api/images/', list_images, name='list_images'),
    path('api/openai-chat/', openai_chat, name='openai_chat'),
    # LEAI course management
    path('api/create_course/', create_course, name='create_course'),
    path('api/verify_course_password/', verify_course_password, name='verify_course_password'),
    path('api/create_feedback_gpt/', create_feedback_gpt, name='create_feedback_gpt'),
    path('api/feedback_gpts_by_course/', feedback_gpts_by_course, name='feedback_gpts_by_course'),
    path('api/get_feedback_gpt_by_public_id/', get_feedback_gpt_by_public_id, name='get_feedback_gpt_by_public_id'),
    path('api/feedback_messages_by_gpt/', feedback_messages_by_gpt, name='feedback_messages_by_gpt'),
    path('api/feedback_messages_by_course/', feedback_messages_by_course, name='feedback_messages_by_course'),
    # LEAI survey lifecycle & management
    path('api/set_survey_status/', set_survey_status, name='set_survey_status'),
    path('api/update_survey/', update_survey, name='update_survey'),
    path('api/clone_survey/', clone_survey, name='clone_survey'),
    path('api/export_survey_responses/', export_survey_responses, name='export_survey_responses'),
    path('api/get_group_session/', get_group_session, name='get_group_session'),
]
