"""Mount everything with one line in your root URLconf::

    path("", include("fingerprint_attendance.urls")),

This serves the ADMS endpoints at ``/<ADMS_URL_PREFIX>`` (default ``/iclock/``) and the REST
API at ``/<API_URL_PREFIX>v1/`` (default ``/api/fingerprint/v1/``) under the
``API_URL_NAMESPACE`` namespace. Include ``fingerprint_attendance.adms.urls`` and
``fingerprint_attendance.api.urls`` separately for full control.
"""

from __future__ import annotations

from django.urls import include, path

from .conf import settings

urlpatterns = [
    path("", include("fingerprint_attendance.adms.urls")),
    path(settings.API_URL_PREFIX,
         include(("fingerprint_attendance.api.urls", "fingerprint_attendance_api"),
                 namespace=settings.API_URL_NAMESPACE)),
]
