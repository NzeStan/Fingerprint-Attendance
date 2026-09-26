from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Queue missing users/templates on every active device (--full: push everything)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--full", action="store_true",
                            help="Forget what devices are believed to hold; push everything.")

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.services import resync_all

        result = resync_all(full=options["full"])
        self.stdout.write(f"{result['queued']} command(s) queued on {result['devices']} "
                          f"device(s)")
