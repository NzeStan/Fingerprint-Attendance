"""Public Python service layer.

Every business operation is available here without HTTP, so you can use the package from
your own views, admin actions, Celery tasks or scripts. The REST API calls these functions.

    from fingerprint_attendance import services

    enrollee = services.create_enrollee(employee)
    services.give_consent(enrollee, version="2026-01")
    session = services.start_enrollment_session(enrollee, device=device, fingers=[6])
"""

from __future__ import annotations

from ..ingestion.candidates import PunchCandidate
from ..ingestion.pipeline import IngestResult, ingest_punches
from ..sync.engine import (
    backfill_device,
    reconcile_device_scope,
    remove_enrollee_from_devices,
    resync_all,
    resync_device,
    resync_enrollee,
    sync_enrollee,
    sync_status_for_device,
    sync_status_for_enrollee,
)
from .attendance import calendar_changed, generate_absences, process_pairs, recompute
from .audit import log_action
from .commands import (
    cancel_command,
    expire_commands,
    fetch_commands,
    queue_command,
    record_results,
    retry_command,
    retry_unacknowledged,
)
from .consent import give_consent, withdraw_consent
from .devices import (
    approve_device,
    clear_device_data,
    clear_device_logs,
    device_health,
    disable_device,
    health_summary,
    issue_device_token,
    mark_offline_devices,
    reconciliation_report,
    record_clock_drift,
    register_device,
    reset_upload_cursor,
)
from .enrollees import (
    activate_enrollee,
    create_enrollee,
    deactivate_enrollee,
    delete_enrollee,
    link_unknown_punches,
    update_enrollee,
)
from .enrollment import (
    agent_start_session,
    authenticate_agent,
    cancel_session,
    claim_session,
    complete_session,
    create_agent,
    expire_sessions,
    rotate_agent_key,
    start_enrollment_session,
    upload_agent_template,
)
from .punches import adjust_punch, create_manual_punch, import_punches
from .retention import purge_retention
from .templates import (
    delete_all_templates,
    delete_template,
    read_template_data,
    receive_device_template,
    store_template,
)


def send_command(device, command_type, payload=None, **kwargs):  # type: ignore[no-untyped-def]
    """Alias of :func:`queue_command` for readability in consumer code."""
    return queue_command(device, command_type, payload, **kwargs)


__all__ = [
    "IngestResult",
    "PunchCandidate",
    "activate_enrollee",
    "adjust_punch",
    "agent_start_session",
    "approve_device",
    "authenticate_agent",
    "backfill_device",
    "calendar_changed",
    "cancel_command",
    "cancel_session",
    "claim_session",
    "clear_device_data",
    "clear_device_logs",
    "complete_session",
    "create_agent",
    "create_enrollee",
    "create_manual_punch",
    "deactivate_enrollee",
    "delete_all_templates",
    "delete_enrollee",
    "delete_template",
    "device_health",
    "disable_device",
    "expire_commands",
    "expire_sessions",
    "fetch_commands",
    "generate_absences",
    "give_consent",
    "health_summary",
    "import_punches",
    "ingest_punches",
    "issue_device_token",
    "link_unknown_punches",
    "log_action",
    "mark_offline_devices",
    "process_pairs",
    "purge_retention",
    "queue_command",
    "read_template_data",
    "receive_device_template",
    "recompute",
    "reconcile_device_scope",
    "reconciliation_report",
    "record_clock_drift",
    "record_results",
    "register_device",
    "remove_enrollee_from_devices",
    "reset_upload_cursor",
    "resync_all",
    "resync_device",
    "resync_enrollee",
    "retry_command",
    "retry_unacknowledged",
    "rotate_agent_key",
    "send_command",
    "start_enrollment_session",
    "store_template",
    "sync_enrollee",
    "sync_status_for_device",
    "sync_status_for_enrollee",
    "update_enrollee",
    "upload_agent_template",
    "withdraw_consent",
]
