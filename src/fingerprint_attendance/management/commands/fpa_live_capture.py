from __future__ import annotations

import signal
import threading
from typing import Any

from django.core.management.base import CommandError

from ._base import DeviceCommandMixin


class Command(DeviceCommandMixin):
    help = ("Long-running live capture for pull-mode devices, with reconnect/backoff, gap-fill "
            "after reconnects and graceful shutdown (SIGINT/SIGTERM).")

    def add_arguments(self, parser: Any) -> None:
        self.add_device_argument(parser)
        parser.add_argument("--timeout", type=int, default=10)

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.conf import settings
        from fingerprint_attendance.constants import DeviceMode, DeviceStatus
        from fingerprint_attendance.pull.service import LiveCapture

        if not settings.PULL_ENABLED:
            raise CommandError("PULL_ENABLED is off")
        devices = self.get_devices(options["devices"], mode=DeviceMode.PULL,
                                   status=DeviceStatus.ACTIVE)
        if not devices:
            raise CommandError("no active pull-mode devices")
        captures = [LiveCapture(d, timeout=options["timeout"]) for d in devices]
        stop = threading.Event()

        def shutdown(*_: Any) -> None:
            stop.set()
            for capture in captures:
                capture.stop()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        threads = [threading.Thread(target=c.run, name=f"fpa-live-{c.device.serial_number}",
                                    daemon=True) for c in captures]
        for thread in threads:
            thread.start()
        self.stdout.write(f"live capture running on {len(threads)} device(s); Ctrl+C to stop")
        while not stop.is_set() and any(t.is_alive() for t in threads):
            stop.wait(1)
        for thread in threads:
            thread.join(timeout=15)
        self.stdout.write("live capture stopped")
