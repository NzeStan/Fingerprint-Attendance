from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Recompute AttendanceDay rows for a date range."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
        parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
        parser.add_argument("--enrollee", action="append", default=[],
                            help="Device PIN or enrollee id (repeatable)")

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.conf import settings
        from fingerprint_attendance.models import Enrollee
        from fingerprint_attendance.services import recompute

        if not settings.ATTENDANCE_PROCESSOR:
            raise CommandError("ATTENDANCE_PROCESSOR is disabled")
        try:
            start, end = date.fromisoformat(options["start"]), date.fromisoformat(options["end"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if end < start:
            raise CommandError("--to is before --from")
        ids = None
        if options["enrollee"]:
            ids = []
            for ref in options["enrollee"]:
                enrollee = Enrollee.objects.filter(device_pin=ref).first()
                if enrollee is None:
                    try:
                        enrollee = Enrollee.objects.filter(uuid=uuid.UUID(ref)).first()
                    except ValueError:
                        enrollee = None
                if enrollee is None:
                    raise CommandError(f"unknown enrollee {ref}")
                ids.append(enrollee.pk)
        count = recompute(start=start, end=end, enrollee_ids=ids)
        self.stdout.write(f"{count} attendance day(s) computed")
