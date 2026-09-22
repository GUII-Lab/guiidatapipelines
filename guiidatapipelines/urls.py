from django.contrib import admin
from django.urls import include, path
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path("datapipeline/api/v1/", include("leai.api.urls")),
    path("datapipeline/", include("datapipeline.urls")),
    path('admin/', admin.site.urls),
]

# Serve media files during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
