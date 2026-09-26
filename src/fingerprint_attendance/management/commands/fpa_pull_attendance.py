from __future__ import annotations

from typing import Any

from django.core.management.base import CommandError

from ._base import DeviceCommandMixin


class Command(DeviceCommandMixin):
    help = "Import punches from pull-mode devices (new records only; --full re-imports all)."

    def add_arguments(self, parser: Any) -> None:
        self.add_device_argument(parser)
        parser.add_argument("--full", action="store_true",
                            help="Re-import the whole device log (dedupe prevents duplicates)")
        parser.add_argument("--loop", action="store_true",
                            help="Keep polling every PULL_POLL_INTERVAL")

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.conf import settings
        from fingerprint_attendance.constants import DeviceMode, DeviceStatus
        from fingerprint_attendance.pull.base import PullError
        from fingerprint_attendance.pull.service import import_attendance, poll_forever

        if not settings.PULL_ENABLED:
            raise CommandError("PULL_ENABLED is off")
        devices = self.get_devices(options["devices"], mode=DeviceMode.PULL,
                                   status=DeviceStatus.ACTIVE)
        if not devices:
            raise CommandError("no active pull-mode devices")
        if options["loop"]:
            poll_forever(devices)
            return
        failed = 0
        for device in devices:
            try:
                result = import_attendance(device, full=options["full"])
                self.stdout.write(f"{device.serial_number}: {result.created_count} new, "
                                  f"{result.duplicates} duplicate(s)")
            except PullError as exc:
                failed += 1
                self.stderr.write(f"{device.serial_number}: {exc}")
        if failed:
            raise CommandError(f"{failed} device(s) failed")
