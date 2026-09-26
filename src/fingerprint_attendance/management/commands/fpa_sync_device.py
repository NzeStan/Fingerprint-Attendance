from __future__ import annotations

from typing import Any

from django.core.management.base import CommandError

from ._base import DeviceCommandMixin


class Command(DeviceCommandMixin):
    help = ("Push missing users/templates to devices. Pull-mode devices are written directly; "
            "push (ADMS) devices get queued commands.")

    def add_arguments(self, parser: Any) -> None:
        self.add_device_argument(parser)
        parser.add_argument("--full", action="store_true", help="Push everything (ADMS)")
        parser.add_argument("--remove-extra", action="store_true",
                            help="Delete users unknown to the server (pull mode)")

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.constants import DeviceMode, DeviceStatus
        from fingerprint_attendance.pull.base import PullError
        from fingerprint_attendance.pull.service import sync_device
        from fingerprint_attendance.services import resync_device

        devices = self.get_devices(options["devices"], status=DeviceStatus.ACTIVE)
        if not devices:
            raise CommandError("no active devices")
        for device in devices:
            if device.mode == DeviceMode.PULL:
                try:
                    result = sync_device(device, remove_extra=options["remove_extra"])
                except PullError as exc:
                    self.stderr.write(f"{device.serial_number}: {exc}")
                    continue
            else:
                result = resync_device(device, full=options["full"])
            self.stdout.write(f"{device.serial_number}: {result}")
