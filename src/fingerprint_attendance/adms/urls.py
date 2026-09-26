"""ADMS URLs. Mount at the site root so devices can reach ``/<ADMS_URL_PREFIX>cdata``::

    path("", include("fingerprint_attendance.adms.urls"))

Firmware variously calls ``cdata``, ``cdata.aspx`` and adds trailing slashes; all are accepted.
"""

from __future__ import annotations

import re

from django.urls import re_path

from ..conf import settings
from . import views

app_name = "fpa_adms"

_prefix = re.escape(settings.ADMS_URL_PREFIX.strip("/"))
_p = f"^{_prefix}/" if _prefix else "^"

urlpatterns = [
    re_path(rf"{_p}cdata(?:\.aspx)?/?$", views.cdata, name="cdata"),
    re_path(rf"{_p}getrequest(?:\.aspx)?/?$", views.getrequest, name="getrequest"),
    re_path(rf"{_p}devicecmd(?:\.aspx)?/?$", views.devicecmd, name="devicecmd"),
    re_path(rf"{_p}ping(?:\.aspx)?/?$", views.ping, name="ping"),
    re_path(rf"{_p}registry(?:\.aspx)?/?$", views.registry, name="registry"),
    re_path(rf"{_p}push(?:\.aspx)?/?$", views.push, name="push"),
    re_path(rf"{_p}querydata(?:\.aspx)?/?$", views.querydata, name="querydata"),
    re_path(rf"{_p}rtdata(?:\.aspx)?/?$", views.rtdata, name="rtdata"),
    re_path(rf"{_p}[A-Za-z]+(?:\.aspx)?/?$", views.fallback, name="fallback"),
]
