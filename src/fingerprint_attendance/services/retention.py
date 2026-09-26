"""Scheduled data retention. Template deletions propagate to devices first."""

from __future__ import annotations

from datetime import timedelta

from ..conf import settings
from ..constants import FINAL_COMMAND_STATUSES, DeliveryStatus
from ..utils.logging import get_logger
from ..utils.timeutils import now
from .audit import log_action

logger = get_logger(__name__)


def purge_retention(*, dry_run: bool = False) -> dict[str, int]:
    from ..models import (
        AuditLog,
        DeviceCommand,
        DeviceEventLog,
        FingerprintTemplate,
        Punch,
        WebhookDelivery,
    )
    from .templates import delete_template

    current = now()
    counts: dict[str, int] = {}

    after = settings.RETENTION_DELETE_TEMPLATES_AFTER_DEACTIVATION
    if after is not None:
        templates = FingerprintTemplate.objects.filter(
            enrollee__is_active=False, enrollee__deactivated_at__lte=current - after
        ).select_related("enrollee")
        counts["templates"] = templates.count()
        if not dry_run:
            for template in templates:
                # devices were already told at deactivation; propagate again to be safe
                delete_template(template, propagate=True, reason="retention")

    def purge(name: str, qs: object) -> None:
        counts[name] = qs.count()  # type: ignore[attr-defined]
        if not dry_run and counts[name]:
            qs.delete()  # type: ignore[attr-defined]

    if settings.RETENTION_PUNCHES_DAYS is not None:
        purge("punches", Punch.objects.filter(
            punched_at__lt=current - timedelta(days=settings.RETENTION_PUNCHES_DAYS)))
    if settings.RETENTION_EVENT_LOG_DAYS is not None:
        purge("event_logs", DeviceEventLog.objects.filter(
            created_at__lt=current - timedelta(days=settings.RETENTION_EVENT_LOG_DAYS)))
    if settings.RETENTION_COMMANDS_DAYS is not None:
        purge("commands", DeviceCommand.objects.filter(
            status__in=FINAL_COMMAND_STATUSES,
            updated_at__lt=current - timedelta(days=settings.RETENTION_COMMANDS_DAYS)))
    if settings.RETENTION_WEBHOOK_DELIVERIES_DAYS is not None:
        purge("webhook_deliveries", WebhookDelivery.objects.filter(
            status__in=[DeliveryStatus.SUCCEEDED, DeliveryStatus.FAILED],
            created_at__lt=current - timedelta(days=settings.RETENTION_WEBHOOK_DELIVERIES_DAYS)))
    if settings.RETENTION_AUDIT_LOG_DAYS is not None:
        purge("audit_log", AuditLog.objects.filter(
            created_at__lt=current - timedelta(days=settings.RETENTION_AUDIT_LOG_DAYS)))

    if not dry_run and any(counts.values()):
        log_action("retention.purge", metadata=counts)
    return counts
