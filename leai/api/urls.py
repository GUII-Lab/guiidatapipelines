from django.urls import path

from .environment import environment_view


urlpatterns = [
    path("environment/", environment_view, name="leai-environment"),
]
