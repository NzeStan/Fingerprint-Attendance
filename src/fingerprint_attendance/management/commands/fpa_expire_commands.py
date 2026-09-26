from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Expire queued device commands past their COMMAND_EXPIRY and stale sessions."

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.services import expire_commands, expire_sessions

        self.stdout.write(f"{expire_commands()} command(s) expired")
        self.stdout.write(f"{expire_sessions()} enrollment session(s) expired")
