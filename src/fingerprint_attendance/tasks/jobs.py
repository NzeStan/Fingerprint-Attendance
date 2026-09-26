"""Task functions (JSON-serializable arguments) run through the task backend.

Periodic jobs are listed in :data:`fingerprint_attendance.tasks.celery.PERIODIC_JOBS`; without
Celery, run the equivalent management commands from cron.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def check_devices() -> int:
    from ..services.devices import mark_offline_devices

    return mark_offline_devices()


def expire_commands() -> int:
    from ..services.commands import expire_commands as _expire

    return _expire()


def retry_commands() -> int:
    from ..services.commands import retry_unacknowledged

    return retry_unacknowledged()


def expire_enrollment_sessions() -> int:
    from ..services.enrollment import expire_sessions

    return expire_sessions()


def retry_webhook_deliveries() -> int:
    from ..conf import settings

    if not settings.WEBHOOKS_ENABLED:
        return 0
    from ..webhooks.delivery import retry_due

    return retry_due()


def purge_retention() -> dict[str, int]:
    from ..services.retention import purge_retention as _purge

    return _purge()


def generate_absences(day: str | None = None) -> int:
    from ..services.attendance import generate_absences as _generate

    return _generate(date.fromisoformat(day) if day else None)


def process_attendance(pairs: list[list[Any]]) -> int:
    from ..services.attendance import process_pairs

    return process_pairs([(p[0], p[1]) for p in pairs])


def recompute_attendance(start: str, end: str, enrollee_ids: list[int] | None = None) -> int:
    from ..services.attendance import recompute

    return recompute(start=date.fromisoformat(start), end=date.fromisoformat(end),
                     enrollee_ids=enrollee_ids)


def backfill_device(device_id: int) -> dict[str, int]:
    from ..models import Device
    from ..sync.engine import backfill_device as _backfill

    device = Device.objects.filter(pk=device_id).first()
    return _backfill(device) if device else {}


def reconcile_device_scope(device_id: int) -> dict[str, int]:
    from ..models import Device
    from ..sync.engine import reconcile_device_scope as _reconcile

    device = Device.objects.filter(pk=device_id).first()
    return _reconcile(device) if device else {}


def sync_enrollee(enrollee_id: int) -> dict[str, int]:
    from ..models import Enrollee
    from ..sync.engine import sync_enrollee as _sync

    enrollee = Enrollee.objects.filter(pk=enrollee_id).first()
    return _sync(enrollee) if enrollee else {}


def deliver_webhook(delivery_id: int) -> bool:
    from ..webhooks.delivery import deliver

    return deliver(delivery_id)
