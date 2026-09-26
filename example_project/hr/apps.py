from __future__ import annotations

from django.apps import AppConfig


class HrConfig(AppConfig):
    name = "hr"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # a custom status used by the custom processor
        from fingerprint_attendance.registry import attendance_statuses

        attendance_statuses.register("overtime", "Worked overtime")
