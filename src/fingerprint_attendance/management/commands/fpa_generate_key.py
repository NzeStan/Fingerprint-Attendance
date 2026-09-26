from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print a new Fernet key for TEMPLATE_ENCRYPTION_KEYS."
    requires_system_checks: list[str] = []

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.crypto import generate_key

        self.stdout.write(generate_key())
