"""Canonical-only URLs for the new LEAI deployment; no legacy API surface."""
from django.urls import include, path

urlpatterns = [path("datapipeline/api/v1/", include("leai.api.urls"))]

# Unknown browser routes use the same branded page as invalid course routes.
# Django only invokes this handler with DEBUG=False, which is the deployed
# combined LEAI configuration. API misses remain ordinary 404 responses.
handler404 = "leai.web_views.page_not_found"
