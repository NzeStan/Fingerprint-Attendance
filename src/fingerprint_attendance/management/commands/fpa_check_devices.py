from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Mark devices that stopped talking as offline (emits device_offline)."

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.services import mark_offline_devices

        self.stdout.write(f"{mark_offline_devices()} device(s) marked offline")
