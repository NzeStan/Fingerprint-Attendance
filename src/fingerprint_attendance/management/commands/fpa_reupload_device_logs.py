from __future__ import annotations

from typing import Any

from django.core.management.base import CommandError
from django.utils.dateparse import parse_datetime

from ._base import DeviceCommandMixin


class Command(DeviceCommandMixin):
    help = ("Reset a device's upload cursor so it re-sends stored punches (safe: duplicates "
            "are dropped). With --from, also asks for that range explicitly.")

    def add_arguments(self, parser: Any) -> None:
        self.add_device_argument(parser, required=True)
        parser.add_argument("--from", dest="start", help="ISO datetime")
        parser.add_argument("--table", action="append", default=[],
                            help="ATTLOG (default), OPERLOG, BIODATA")

    def handle(self, *args: Any, **options: Any) -> None:
        from django.utils import timezone

        from fingerprint_attendance.services import reset_upload_cursor

        start = None
        if options.get("start"):
            start = parse_datetime(options["start"])
            if start is None:
                raise CommandError("--from must be an ISO datetime")
            if timezone.is_naive(start):
                start = timezone.make_aware(start)
        tables = tuple(t.upper() for t in options["table"]) or ("ATTLOG",)
        for device in self.get_devices(options["devices"]):
            commands = reset_upload_cursor(device, tables, from_datetime=start)
            self.stdout.write(f"{device.serial_number}: cursor reset, {len(commands)} "
                              f"command(s) queued")
