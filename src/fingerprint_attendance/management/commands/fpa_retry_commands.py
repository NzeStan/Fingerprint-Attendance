from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Retry commands sent but never acknowledged, and due webhook deliveries."

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.services import retry_unacknowledged
        from fingerprint_attendance.tasks.jobs import retry_webhook_deliveries

        self.stdout.write(f"{retry_unacknowledged()} command(s) rescheduled")
        self.stdout.write(f"{retry_webhook_deliveries()} webhook delivery(ies) retried")
