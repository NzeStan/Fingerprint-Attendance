"""Device lifecycle: registration, approval, authentication, liveness, upload cursors,
clock drift, capacity and reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Count, Max, Q

from .. import payloads
from ..conf import settings
from ..constants import (
    OPEN_COMMAND_STATUSES,
    CommandStatus,
    ConnectionState,
    DeviceStatus,
    EventLogType,
)
from ..events import emit
from ..exceptions import DeviceRejected, NotAllowed, UnsafeOperation
from ..utils.logging import get_logger
from ..utils.security import generate_secret, hash_secret, ip_in, verify_secret
from ..utils.timeutils import device_zone, now, to_utc
from .audit import log_action

logger = get_logger(__name__)

CURSOR_FIELDS = {
    "ATTLOG": "attlog_stamp",
    "OPERLOG": "operlog_stamp",
    "ATTPHOTO": "attphoto_stamp",
    "BIODATA": "biodata_stamp",
}


# --------------------------------------------------------------------------- registration


def register_device(serial_number: str, *, name: str = "", status: str | None = None,
                    by: Any = None, **fields: Any) -> Any:
    from ..models import Device

    if status is None:
        status = (DeviceStatus.PENDING_APPROVAL if settings.ADMS_REQUIRE_DEVICE_APPROVAL
                  else DeviceStatus.ACTIVE)
    device = Device.objects.create(serial_number=serial_number.strip(), name=name,
                                   status=status, **fields)
    if status == DeviceStatus.ACTIVE:
        device.approved_at = now()
        device.save(update_fields=["approved_at"])
    log_action("device.register", actor=by, obj=device)
    emit("device_registered", payloads.device_payload(device), device=device)
    if status == DeviceStatus.ACTIVE and settings.SYNC_ON_DEVICE_APPROVAL:
        _schedule_backfill(device)
    return device


def _schedule_backfill(device: Any) -> None:
    from ..tasks.backends import enqueue

    enqueue("fingerprint_attendance.tasks.jobs.backfill_device", device.pk)


def approve_device(device: Any, *, by: Any = None) -> Any:
    if device.status == DeviceStatus.ACTIVE:
        return device
    device.status = DeviceStatus.ACTIVE
    device.approved_at = now()
    device.approved_by = by if getattr(by, "pk", None) else None
    device.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
    log_action("device.approve", actor=by, obj=device)
    emit("device_approved", payloads.device_payload(device), device=device)
    if settings.SYNC_ON_DEVICE_APPROVAL:
        _schedule_backfill(device)
    return device


def disable_device(device: Any, *, by: Any = None) -> Any:
    device.status = DeviceStatus.DISABLED
    device.save(update_fields=["status", "updated_at"])
    log_action("device.disable", actor=by, obj=device)
    emit("device_disabled", payloads.device_payload(device), device=device)
    return device


def issue_device_token(device: Any, *, by: Any = None) -> str:
    """Create a new device token; returns the plain value (only its hash is stored)."""
    token = generate_secret(24)
    device.token_hash = hash_secret(token)
    device.save(update_fields=["token_hash", "updated_at"])
    log_action("device.issue_token", actor=by, obj=device)
    return token


def resolve_adms_device(serial: str, *, ip: str | None, token: str | None) -> Any:
    """Authenticate an ADMS request; auto-registers unknown devices when enabled.

    Raises :class:`DeviceRejected` for disabled/unknown devices or failed checks.
    """
    from ..models import Device, DeviceEventLog

    allowed_ips = settings.ADMS_ALLOWED_IPS or []
    if allowed_ips and not ip_in(ip, allowed_ips):
        raise DeviceRejected("IP address not allowed", code="ip_not_allowed")
    device = Device.objects.filter(serial_number=serial).first()
    if device is None:
        if not settings.ADMS_AUTO_REGISTER_DEVICES:
            raise DeviceRejected("unknown device", code="unknown_device")
        if settings.ADMS_DEVICE_TOKEN_REQUIRED:
            raise DeviceRejected("device must be registered before connecting",
                                 code="unknown_device")
        with transaction.atomic():
            device = Device.objects.select_for_update().filter(serial_number=serial).first()
            if device is None:
                device = register_device(serial, last_ip=ip)
    if device.status == DeviceStatus.DISABLED:
        raise DeviceRejected("device disabled", code="device_disabled")
    if settings.ADMS_DEVICE_TOKEN_REQUIRED and not verify_secret(token, device.token_hash):
        DeviceEventLog.objects.create(device=device, event_type=EventLogType.AUTH_FAILED,
                                      message="invalid or missing device token",
                                      data={"ip": ip})
        raise DeviceRejected("invalid device token", code="invalid_token")
    return device


# --------------------------------------------------------------------------- liveness


def touch_device(device: Any, *, ip: str | None = None, push_version: str | None = None,
                 info: dict[str, Any] | None = None, handshake: bool = False) -> Any:
    """Record that the device contacted us; update reported info and online state."""
    current = now()
    was_online = device.connection_state == ConnectionState.ONLINE
    fields = {"last_seen_at", "connection_state", "updated_at"}
    device.last_seen_at = current
    device.connection_state = ConnectionState.ONLINE
    if ip and ip != device.last_ip:
        device.last_ip = ip
        fields.add("last_ip")
    if push_version and push_version != device.push_version:
        device.push_version = push_version[:32]
        fields.add("push_version")
    if handshake:
        device.last_handshake_at = current
        fields.add("last_handshake_at")
    if info:
        from ..adms.adapter import get_adapter

        for key, value in get_adapter(device).device_updates_from_info(info).items():
            if getattr(device, key) != value:
                setattr(device, key, value)
                fields.add(key)
    if not was_online:
        device.went_offline_at = None
        fields.add("went_offline_at")
    device.save(update_fields=sorted(fields))
    if not was_online:
        emit("device_online", payloads.device_payload(device), device=device)
    if info:
        check_log_capacity(device)
    if handshake:
        emit("device_handshake", payloads.device_payload(device), device=device)
    return device


def apply_device_info(device: Any, info: dict[str, Any]) -> Any:
    """Apply an ``INFO`` command result or ``table=options`` upload."""
    from ..adms.adapter import get_adapter
    from ..models import DeviceEventLog

    updates = get_adapter(device).device_updates_from_info(info)
    for key, value in updates.items():
        setattr(device, key, value)
    caps = dict(device.capabilities or {})
    caps["info"] = {k: v for k, v in info.items() if len(str(v)) < 200}
    device.capabilities = caps
    device.save()
    DeviceEventLog.objects.create(device=device, event_type=EventLogType.INFO,
                                  message="device information updated",
                                  data={"updated": sorted(updates)})
    check_log_capacity(device)
    return device


def mark_offline_devices() -> int:
    from ..models import Device

    cutoff = now() - settings.DEVICE_OFFLINE_AFTER
    count = 0
    for device in Device.objects.filter(connection_state=ConnectionState.ONLINE,
                                        last_seen_at__lt=cutoff):
        device.connection_state = ConnectionState.OFFLINE
        device.went_offline_at = device.last_seen_at
        device.save(update_fields=["connection_state", "went_offline_at", "updated_at"])
        emit("device_offline", payloads.device_payload(device), device=device)
        count += 1
    return count


# --------------------------------------------------------------------------- cursors


def advance_cursor(device: Any, table: str, stamp: str | None) -> None:
    """Store the device's upload cursor. Call inside the transaction that saved the batch so
    the cursor only moves once the data is committed."""
    field = CURSOR_FIELDS.get(table.upper())
    if not field or not stamp:
        return
    stamp = str(stamp)[:32]
    if getattr(device, field) != stamp:
        setattr(device, field, stamp)
        type(device).objects.filter(pk=device.pk).update(**{field: stamp})


def reset_upload_cursor(device: Any, tables: tuple[str, ...] = ("ATTLOG",), *,
                        from_datetime: datetime | None = None, to_datetime: datetime | None = None,
                        by: Any = None) -> list[Any]:
    """Make the device re-send its stored history. Dedupe makes this safe.

    Resets the handshake stamps (the device re-uploads on its next handshake) and, when
    ``from_datetime`` is given, also queues ``DATA QUERY ATTLOG`` for that range. Returns the
    queued commands.
    """
    from ..models import DeviceEventLog
    from .commands import queue_command

    updates = {}
    for table in tables:
        field = CURSOR_FIELDS.get(table.upper())
        if field:
            updates[field] = "0"
            setattr(device, field, "0")
    if updates:
        type(device).objects.filter(pk=device.pk).update(**updates)
    commands = []
    if from_datetime is not None:
        zone = device_zone(device)
        end = (to_datetime or now()).astimezone(zone)
        commands.append(queue_command(
            device, "query_attlog",
            {"start": from_datetime.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
             "end": end.strftime("%Y-%m-%d %H:%M:%S")},
            created_by=by, dedupe_key="reupload:attlog"))
    commands.append(queue_command(device, "check", {}, created_by=by, dedupe_key="check"))
    DeviceEventLog.objects.create(device=device, event_type=EventLogType.CURSOR_RESET,
                                  message=f"upload cursor reset for {', '.join(tables)}",
                                  data={"from": from_datetime.isoformat() if from_datetime
                                        else None})
    log_action("device.reupload_logs", actor=by, obj=device,
               metadata={"tables": list(tables),
                         "from": from_datetime.isoformat() if from_datetime else None})
    return commands


# --------------------------------------------------------------------------- clock


def record_clock_drift(device: Any, device_time: datetime, *, source: str = "adms") -> float:
    """Compare device time (naive local or aware) with server time; flag/correct drift."""
    from ..models import DeviceEventLog
    from .commands import queue_command

    device_utc = to_utc(device_time, device_zone(device)) if device_time.tzinfo is None \
        else device_time
    drift = (device_utc - now()).total_seconds()
    device.clock_drift_seconds = drift
    device.clock_checked_at = now()
    device.save(update_fields=["clock_drift_seconds", "clock_checked_at", "updated_at"])
    if abs(drift) > settings.CLOCK_DRIFT_WARNING_SECONDS:
        DeviceEventLog.objects.create(device=device, event_type=EventLogType.CLOCK_DRIFT,
                                      message=f"clock drift {drift:.0f}s ({source})",
                                      data={"drift_seconds": drift, "source": source})
        emit("device_clock_drift", {**payloads.device_payload(device), "drift_seconds": drift},
             device=device, drift_seconds=drift)
        if settings.AUTO_CORRECT_DEVICE_TIME:
            if abs(drift) <= settings.MAX_AUTO_TIME_CORRECTION.total_seconds():
                queue_command(device, "set_time", {}, dedupe_key="set_time")
            else:
                logger.warning("Drift of %.0fs on %s exceeds MAX_AUTO_TIME_CORRECTION; not "
                               "correcting automatically", drift, device.serial_number)
    return drift


# --------------------------------------------------------------------------- counts


def server_punch_count(device: Any) -> int:
    from ..models import Punch

    return Punch.objects.filter(device=device).count()


def pending_upload_estimate(device: Any, server_count: int | None = None) -> int | None:
    """Device-reported log count minus what the server holds since the last device clear."""
    if device.reported_punch_count is None:
        return None
    held = (server_count if server_count is not None else server_punch_count(device)) \
        - (device.log_count_baseline or 0)
    return max(0, int(device.reported_punch_count) - max(held, 0))


def check_log_capacity(device: Any) -> bool:
    """Emit a warning when stored logs approach capacity (edge triggered)."""
    from ..models import DeviceEventLog

    if device.reported_punch_count is None:
        return False
    capacity = device.effective_log_capacity
    used = int(device.reported_punch_count)
    threshold = capacity * settings.LOG_CAPACITY_WARNING_RATIO
    over = used >= threshold
    if over and not device.capacity_warning_active:
        type(device).objects.filter(pk=device.pk).update(capacity_warning_active=True)
        device.capacity_warning_active = True
        DeviceEventLog.objects.create(device=device, event_type=EventLogType.CAPACITY_WARNING,
                                      message=f"log storage at {used}/{capacity}",
                                      data={"used": used, "capacity": capacity})
        emit("device_log_capacity_warning",
             {**payloads.device_payload(device), "used": used, "capacity": capacity},
             device=device, used=used, capacity=capacity)
    elif not over and device.capacity_warning_active:
        type(device).objects.filter(pk=device.pk).update(capacity_warning_active=False)
        device.capacity_warning_active = False
    return over


def reconciliation_report(devices: Any = None) -> list[dict[str, Any]]:
    from ..models import Device

    qs = devices if devices is not None else Device.objects.all()
    qs = qs.annotate(server_count=Count("punches"), last_punch=Max("punches__punched_at"))
    rows = []
    for device in qs:
        held = device.server_count - (device.log_count_baseline or 0)
        pending = pending_upload_estimate(device, device.server_count)
        rows.append({
            "device_id": str(device.uuid),
            "serial_number": device.serial_number,
            "name": device.name,
            "device_reported_count": device.reported_punch_count,
            "server_count": device.server_count,
            "server_count_since_last_clear": held,
            "pending_upload_estimate": pending,
            "in_sync": pending == 0 if pending is not None else None,
            "last_punch_at": device.last_punch.isoformat() if device.last_punch else None,
            "capacity": device.effective_log_capacity,
        })
    return rows


# --------------------------------------------------------------------------- dangerous ops


def clear_device_logs(device: Any, *, by: Any = None) -> Any:
    """Queue ``CLEAR LOG`` only when the server provably holds every record on the device."""
    from .commands import queue_command

    _assert_logs_reconciled(device)
    return queue_command(device, "clear_logs", {"server_count": server_punch_count(device)},
                         created_by=by, dedupe_key="clear_logs")


def clear_device_data(device: Any, *, by: Any = None) -> Any:
    """Queue ``CLEAR DATA`` (wipes users, templates and logs). Same safety check as logs; the
    device is re-synced once the command succeeds."""
    from .commands import queue_command

    _assert_logs_reconciled(device)
    return queue_command(device, "clear_data", {"server_count": server_punch_count(device)},
                         created_by=by, dedupe_key="clear_data")


def _assert_logs_reconciled(device: Any) -> None:
    if device.status != DeviceStatus.ACTIVE:
        raise NotAllowed("device is not active")
    pending = pending_upload_estimate(device)
    if pending is None:
        raise UnsafeOperation(
            "the device has not reported its log count yet; ask it for INFO first",
            details={"device_reported_count": None})
    if pending > 0:
        raise UnsafeOperation(
            f"the device still holds {pending} record(s) the server has not received",
            details={"pending_upload_estimate": pending,
                     "device_reported_count": device.reported_punch_count,
                     "server_count": server_punch_count(device)})


def on_device_cleared(device: Any, command_type: str) -> None:
    device.refresh_from_db()
    device.log_count_baseline = server_punch_count(device)
    device.reported_punch_count = 0
    device.attlog_stamp = ""
    device.capacity_warning_active = False
    device.save()
    if command_type == "clear_data":
        from ..sync.engine import resync_device

        device.reported_user_count = 0
        device.reported_template_count = 0
        device.save()
        resync_device(device, full=True)


# --------------------------------------------------------------------------- health


def device_health(device: Any, *, server_count: int | None = None,
                  pending_commands: int | None = None, last_punch: Any = None) -> dict[str, Any]:
    from ..models import DeviceCommand

    if pending_commands is None:
        pending_commands = DeviceCommand.objects.filter(
            device=device, status__in=OPEN_COMMAND_STATUSES).count()
    offline_for: timedelta | None = device.offline_duration
    return {
        "device_id": str(device.uuid),
        "serial_number": device.serial_number,
        "name": device.name,
        "status": device.status,
        "is_online": device.is_online,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "offline_seconds": offline_for.total_seconds() if offline_for else None,
        "pending_commands": pending_commands,
        "last_punch_at": (last_punch or device.last_punch_at).isoformat()
        if (last_punch or device.last_punch_at) else None,
        "pending_upload_estimate": pending_upload_estimate(device, server_count),
        "clock_drift_seconds": device.clock_drift_seconds,
        "log_capacity_warning": device.capacity_warning_active,
        "reported_punch_count": device.reported_punch_count,
    }


def health_summary() -> dict[str, Any]:
    from ..constants import SyncStatus
    from ..models import Device, DeviceEnrolleeSync, Enrollee, Punch

    devices = list(Device.objects.annotate(
        server_count=Count("punches"),
        open_commands=Count("commands", filter=Q(commands__status__in=OPEN_COMMAND_STATUSES)),
    ))
    online = sum(1 for d in devices if d.is_online)
    from django.utils import timezone as dj_tz

    today_start = dj_tz.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    unsynced = (DeviceEnrolleeSync.objects
                .filter(enrollee__is_active=True)
                .exclude(status__in=[SyncStatus.SYNCED, SyncStatus.DELETED,
                                     SyncStatus.OUT_OF_SCOPE])
                .values("enrollee").distinct().count())
    from ..models import DeviceCommand

    return {
        "devices": {
            "total": len(devices),
            "online": online,
            "offline": len(devices) - online,
            "pending_approval": sum(1 for d in devices
                                    if d.status == DeviceStatus.PENDING_APPROVAL),
        },
        "commands": {
            "pending": DeviceCommand.objects.filter(status=CommandStatus.PENDING).count(),
            "sent": DeviceCommand.objects.filter(status=CommandStatus.SENT).count(),
            "failed": DeviceCommand.objects.filter(status=CommandStatus.FAILED).count(),
        },
        "enrollees": {
            "active": Enrollee.objects.filter(is_active=True).count(),
            "unsynced": unsynced,
        },
        "punches": {
            "today": Punch.objects.filter(punched_at__gte=today_start).count(),
            "unknown_pin_today": Punch.objects.filter(punched_at__gte=today_start)
            .with_flag("unknown_pin").count(),
        },
        "device_health": [
            device_health(d, server_count=d.server_count, pending_commands=d.open_commands)
            for d in devices
        ],
    }
