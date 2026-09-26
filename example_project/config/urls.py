from __future__ import annotations

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from hr.views import webhook_receiver

urlpatterns = [
    path("admin/", admin.site.urls),
    # ADMS (/iclock/...) + REST API (/api/fingerprint/v1/...)
    path("", include("fingerprint_attendance.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema")),
    # a sample receiver for the package's outbound webhooks
    path("hooks/attendance/", webhook_receiver),
]
