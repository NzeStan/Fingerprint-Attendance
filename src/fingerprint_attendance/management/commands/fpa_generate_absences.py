from __future__ import annotations

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create absent/leave/off AttendanceDay rows for a date (default: yesterday)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
        parser.add_argument("--force", action="store_true", help="Ignore ABSENCE_CUTOFF_TIME")

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.services import generate_absences

        day = date.fromisoformat(options["date"]) if options.get("date") else None
        count = generate_absences(day, force=options["force"])
        self.stdout.write(f"{count} attendance day(s) generated")
