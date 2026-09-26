from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class FingerprintAttendanceConfig(AppConfig):
    name = "fingerprint_attendance"
    label = "fingerprint_attendance"
    verbose_name = _("Fingerprint attendance")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from . import checks  # noqa: F401  (registers system checks)
        from . import receivers  # noqa: F401  (connects internal signal receivers)
        from .utils.logging import install_redaction

        install_redaction()
