from __future__ import annotations

"""Shared helpers for management commands."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError


class DeviceCommandMixin(BaseCommand):
    def add_device_argument(self, parser: Any, *, required: bool = False) -> None:
        parser.add_argument("--device", action="append", dest="devices", default=[],
                            help="Serial number (repeatable). Default: all matching devices.",
                            required=required)

    def get_devices(self, serials: list[str], **filters: Any) -> list[Any]:
        from fingerprint_attendance.models import Device

        qs = Device.objects.filter(**filters)
        if serials:
            qs = qs.filter(serial_number__in=serials)
            found = set(qs.values_list("serial_number", flat=True))
            missing = sorted(set(serials) - found)
            if missing:
                raise CommandError(f"unknown device(s): {', '.join(missing)}")
        return list(qs.order_by("serial_number"))
