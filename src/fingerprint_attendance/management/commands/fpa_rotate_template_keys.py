from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = ("Re-encrypt every template with the first key in TEMPLATE_ENCRYPTION_KEYS "
            "(also encrypts rows stored while encryption was off). Remove the old key "
            "afterwards.")

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.crypto import get_storage
        from fingerprint_attendance.models import FingerprintTemplate
        from fingerprint_attendance.services import log_action

        storage = get_storage()
        changed = 0
        ids = list(FingerprintTemplate.objects.values_list("pk", flat=True))
        size = options["batch_size"]
        for i in range(0, len(ids), size):
            with transaction.atomic():
                for template in FingerprintTemplate.objects.select_for_update().filter(
                        pk__in=ids[i:i + size]):
                    if storage.rotate(template):
                        template.save(update_fields=["template_data", "updated_at"])
                        changed += 1
        log_action("templates.rotate_keys", metadata={"rotated": changed, "total": len(ids)})
        self.stdout.write(f"re-encrypted {changed} of {len(ids)} template(s)")
