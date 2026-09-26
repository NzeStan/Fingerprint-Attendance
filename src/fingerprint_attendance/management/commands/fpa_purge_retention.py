from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Apply the RETENTION_* settings (template deletions propagate to devices)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Only report counts.")

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.services import purge_retention

        counts = purge_retention(dry_run=options["dry_run"])
        prefix = "would delete" if options["dry_run"] else "deleted"
        for name, count in sorted(counts.items()):
            self.stdout.write(f"{prefix} {count} {name}")
