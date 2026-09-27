"""Canonical-only URLs for the new LEAI deployment; no legacy API surface."""
from django.urls import include, path

urlpatterns = [path("datapipeline/api/v1/", include("leai.api.urls"))]
