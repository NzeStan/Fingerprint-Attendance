"""Template sync engine: keeps every in-scope device holding the right users and templates.

State lives in ``DeviceEnrolleeSync`` (what each device has) and is advanced only when the
device acknowledges a command, so the "is everyone on every device?" view is trustworthy.
All operations are idempotent: re-running them never queues duplicate pending commands
(``dedupe_key`` on the command queue) and stale pending commands are superseded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from django.db import transaction

from .. import payloads
from ..conf import settings
from ..constants import OPEN_COMMAND_STATUSES, SyncStatus
from ..events import emit
from ..models import normalize_algorithm
from ..utils.logging import get_logger
from ..utils.timeutils import now
from .strategies import get_sync_strategy

logger = get_logger(__name__)

USER_KEY = "_user"
SYNC_COMMANDS = ("add_user", "add_template", "delete_user", "delete_template")


# --------------------------------------------------------------------------- helpers


def template_key(finger_index: int, algorithm_version: str) -> str:
    return f"{int(finger_index)}:{normalize_algorithm(algorithm_version) or algorithm_version}"


def compatible_algorithms(device: Any) -> set[str] | None:
    """Template algorithms ``device`` accepts, or ``None`` when the device never reported
    one."""
    major = device.algorithm_major
    if not major:
        return None
    mapping = {normalize_algorithm(k): v for k, v in
               (settings.ALGORITHM_COMPATIBILITY_MAP or {}).items()}
    accepted = {major}
    extra = mapping.get(major)
    if extra:
        accepted |= {normalize_algorithm(a) for a in
                     (extra if isinstance(extra, (list, tuple)) else [extra])}
    return accepted


def is_compatible(device: Any, algorithm_version: str) -> bool:
    accepted = compatible_algorithms(device)
    if accepted is None:
        return bool(settings.SYNC_TO_UNKNOWN_ALGORITHM_DEVICES)
    return normalize_algorithm(algorithm_version) in accepted


def choose_templates(device: Any, templates: Iterable[Any]) -> list[Any]:
    """One template per finger: compatible only, preferring the device's own algorithm."""
    own = device.algorithm_major
    best: dict[int, Any] = {}
    for t in templates:
        if not t.is_valid or not is_compatible(device, t.algorithm_version):
            continue
        current = best.get(t.finger_index)
        rank = (normalize_algorithm(t.algorithm_version) == own, t.updated_at)
        if current is None or rank > (normalize_algorithm(current.algorithm_version) == own,
                                      current.updated_at):
            best[t.finger_index] = t
    return [best[k] for k in sorted(best)]


def user_payload(enrollee: Any) -> dict[str, Any]:
    from ..services.enrollees import device_name_for

    body = {"pin": enrollee.device_pin, "name": device_name_for(enrollee),
            "privilege": int(enrollee.privilege)}
    body["user_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return body


def _record(device: Any, enrollee: Any) -> Any:
    from ..models import DeviceEnrolleeSync

    record, _ = DeviceEnrolleeSync.objects.get_or_create(device=device, enrollee=enrollee)
    return record


def _queue(device: Any, command_type: str, payload: dict[str, Any], *, enrollee: Any,
           dedupe_key: str, created_by: Any = None) -> Any:
    from ..services.commands import queue_command

    return queue_command(device, command_type, payload, enrollee=enrollee,
                         dedupe_key=dedupe_key, created_by=created_by, audit=False)


def _has_open_sync_commands(device: Any, enrollee: Any) -> bool:
    from ..models import DeviceCommand

    return DeviceCommand.objects.filter(device=device, enrollee=enrollee,
                                        command_type__in=SYNC_COMMANDS,
                                        status__in=OPEN_COMMAND_STATUSES).exists()


# --------------------------------------------------------------------------- fan-out


def sync_enrollee(enrollee: Any, *, devices: Iterable[Any] | None = None,
                  exclude_devices: Iterable[Any] = (), force: bool = False,
                  created_by: Any = None) -> dict[str, int]:
    """Push the enrollee's user record and templates to every device in scope.

    With ``devices=None`` the strategy decides the scope and devices that are no longer in
    scope get delete commands. Returns counters.
    """
    from ..models import DeviceEnrolleeSync

    summary = {"devices": 0, "queued": 0, "removed": 0, "incompatible": 0}
    if not enrollee.is_active:
        return summary
    excluded = {d.pk for d in exclude_devices}
    full_scope = devices is None
    targets = list(get_sync_strategy().devices_for(enrollee) if full_scope else devices or [])
    templates = list(enrollee.templates.all())
    for device in targets:
        if device.pk in excluded:
            continue
        queued, incompatible = _sync_to_device(enrollee, device, templates, force=force,
                                               created_by=created_by)
        summary["devices"] += 1
        summary["queued"] += queued
        summary["incompatible"] += int(incompatible)
    if full_scope:
        target_ids = {d.pk for d in targets} | excluded
        stale = (DeviceEnrolleeSync.objects.filter(enrollee=enrollee)
                 .exclude(device_id__in=target_ids)
                 .exclude(status__in=[SyncStatus.DELETED, SyncStatus.OUT_OF_SCOPE]))
        for record in stale.select_related("device"):
            if record.user_synced or record.synced_templates or _has_open_sync_commands(
                    record.device, enrollee):
                _delete_user_on(record.device, enrollee, record, reason="out of scope")
                summary["removed"] += 1
            else:
                record.status = SyncStatus.OUT_OF_SCOPE
                record.save(update_fields=["status", "updated_at"])
    return summary


def _sync_to_device(enrollee: Any, device: Any, templates: list[Any], *, force: bool = False,
                    created_by: Any = None) -> tuple[int, bool]:
    from ..services.commands import cancel_pending

    record = _record(device, enrollee)
    chosen = choose_templates(device, templates)
    valid_templates = [t for t in templates if t.is_valid]
    queued = 0
    if valid_templates and not chosen:
        algos = sorted({t.algorithm_version for t in valid_templates})
        record.status = SyncStatus.INCOMPATIBLE
        record.last_error = (f"device algorithm {device.algorithm_major or 'unknown'} cannot use "
                             f"templates {algos}")
        record.save(update_fields=["status", "last_error", "updated_at"])
        emit("sync_failed", {"device": payloads.device_payload(device),
                             "enrollee": payloads.enrollee_payload(enrollee),
                             "error": record.last_error},
             device=device, enrollee=enrollee, error=record.last_error)
        return 0, True
    if not chosen and not settings.SYNC_USERS_WITHOUT_TEMPLATES and not record.user_synced:
        return 0, False

    with transaction.atomic():
        present = dict(record.synced_templates or {})
        if force:
            present = {}
        user = user_payload(enrollee)
        if force or not record.user_synced or present.get(USER_KEY, {}).get("hash") != \
                user["user_hash"]:
            _queue(device, "add_user", user, enrollee=enrollee,
                   dedupe_key=f"user:{enrollee.pk}", created_by=created_by)
            queued += 1
        wanted_keys = set()
        for t in chosen:
            key = template_key(t.finger_index, t.algorithm_version)
            wanted_keys.add(key)
            have = present.get(key)
            if have and have.get("checksum") == t.checksum:
                continue
            _queue(device, "add_template",
                   {"template_id": t.pk, "pin": enrollee.device_pin, "checksum": t.checksum,
                    "version": t.version, "key": key, "finger_index": t.finger_index},
                   enrollee=enrollee, dedupe_key=f"template:{enrollee.pk}:{t.finger_index}",
                   created_by=created_by)
            queued += 1
        # fingers present on the device that no longer exist on the server
        for key in [k for k in present if k != USER_KEY and k not in wanted_keys]:
            finger = int(key.split(":", 1)[0])
            if any(template_key(t.finger_index, t.algorithm_version) == key for t in chosen):
                continue
            if any(t.finger_index == finger for t in chosen):
                continue  # the new template for this finger overwrites it
            _queue(device, "delete_template",
                   {"pin": enrollee.device_pin, "finger_index": finger, "key": key},
                   enrollee=enrollee, dedupe_key=f"template:{enrollee.pk}:{finger}",
                   created_by=created_by)
            queued += 1
        if not chosen:
            cancel_pending(device, dedupe_prefix=f"template:{enrollee.pk}:", enrollee=enrollee)
        record.status = SyncStatus.PENDING if queued else SyncStatus.SYNCED
        if not queued:
            record.last_synced_at = now()
        record.last_error = ""
        record.save(update_fields=["status", "last_error", "last_synced_at", "updated_at"])
    if queued:
        emit("sync_queued", {"device": payloads.device_payload(device),
                             "enrollee": payloads.enrollee_payload(enrollee), "commands": queued},
             device=device, enrollee=enrollee)
    return queued, False


# --------------------------------------------------------------------------- removal


def _delete_user_on(device: Any, enrollee: Any, record: Any, *, reason: str,
                    created_by: Any = None) -> Any:
    from ..services.commands import cancel_pending

    cancel_pending(device, enrollee=enrollee, command_types=("add_user", "add_template",
                                                             "delete_template"),
                   reason=f"superseded: {reason}")
    # PIN-specific key: a later add_user (e.g. after a PIN change or reactivation) must not
    # supersede this delete; FIFO delivery keeps delete-then-add ordering correct.
    command = _queue(device, "delete_user", {"pin": enrollee.device_pin, "reason": reason},
                     enrollee=enrollee, dedupe_key=f"delete:{enrollee.pk}:{enrollee.device_pin}",
                     created_by=created_by)
    record.status = SyncStatus.DELETING
    record.last_error = ""
    record.save(update_fields=["status", "last_error", "updated_at"])
    return command


def remove_enrollee_from_devices(enrollee: Any, *, reason: str = "", everywhere: bool = False,
                                 created_by: Any = None) -> int:
    """Queue ``delete_user`` on every device that has (or may have) the enrollee.

    ``everywhere=True`` (consent withdrawal, deletion) also targets every active device without
    a sync record, in case the person was enrolled there outside the package.
    """
    from ..constants import DeviceStatus
    from ..models import Device, DeviceEnrolleeSync

    count = 0
    seen = set()
    for record in (DeviceEnrolleeSync.objects.filter(enrollee=enrollee)
                   .exclude(status=SyncStatus.DELETED).select_related("device")):
        seen.add(record.device_id)
        if record.status == SyncStatus.OUT_OF_SCOPE and not record.user_synced:
            continue
        _delete_user_on(record.device, enrollee, record, reason=reason, created_by=created_by)
        count += 1
    if everywhere:
        for device in Device.objects.filter(status=DeviceStatus.ACTIVE).exclude(pk__in=seen):
            record = _record(device, enrollee)
            _delete_user_on(device, enrollee, record, reason=reason, created_by=created_by)
            count += 1
    return count


def remove_template_from_devices(enrollee: Any, finger_index: int, algorithm_version: str, *,
                                 created_by: Any = None) -> int:
    from ..models import DeviceEnrolleeSync

    key = template_key(finger_index, algorithm_version)
    count = 0
    for record in DeviceEnrolleeSync.objects.filter(enrollee=enrollee).select_related("device"):
        # queueing with the same dedupe key supersedes a pending add for this finger
        if key in (record.synced_templates or {}) or _has_open_sync_commands(record.device,
                                                                               enrollee):
            _queue(record.device, "delete_template",
                   {"pin": enrollee.device_pin, "finger_index": int(finger_index), "key": key},
                   enrollee=enrollee, dedupe_key=f"template:{enrollee.pk}:{finger_index}",
                   created_by=created_by)
            record.status = SyncStatus.PENDING
            record.save(update_fields=["status", "updated_at"])
            count += 1
    return count


# --------------------------------------------------------------------------- device scope


def backfill_device(device: Any, *, force: bool = False) -> dict[str, int]:
    """Push every in-scope active enrollee to ``device`` (new device, approval, resync)."""
    from ..constants import DeviceStatus

    summary = {"enrollees": 0, "queued": 0, "incompatible": 0}
    if device.status != DeviceStatus.ACTIVE:
        return summary
    for enrollee in get_sync_strategy().enrollees_for(device).prefetch_related("templates"):
        queued, incompatible = _sync_to_device(enrollee, device, list(enrollee.templates.all()),
                                               force=force)
        summary["enrollees"] += 1
        summary["queued"] += queued
        summary["incompatible"] += int(incompatible)
    return summary


def reconcile_device_scope(device: Any) -> dict[str, int]:
    """After a device's groups change: add newly in-scope enrollees, remove the rest."""
    from ..models import DeviceEnrolleeSync

    summary = backfill_device(device)
    in_scope = set(get_sync_strategy().enrollees_for(device).values_list("pk", flat=True))
    removed = 0
    for record in (DeviceEnrolleeSync.objects.filter(device=device)
                   .exclude(enrollee_id__in=in_scope)
                   .exclude(status__in=[SyncStatus.DELETED, SyncStatus.OUT_OF_SCOPE])
                   .select_related("enrollee")):
        _delete_user_on(device, record.enrollee, record, reason="out of scope")
        removed += 1
    summary["removed"] = removed
    return summary


def resync_device(device: Any, *, full: bool = False, created_by: Any = None) -> dict[str, int]:
    """``full=True`` forgets what the device is believed to hold and pushes everything."""
    from ..models import DeviceEnrolleeSync

    if full:
        DeviceEnrolleeSync.objects.filter(device=device).update(
            user_synced=False, synced_templates={}, status=SyncStatus.PENDING)
    return backfill_device(device, force=full)


def resync_enrollee(enrollee: Any, *, full: bool = False, created_by: Any = None) -> dict[str,
                                                                                           int]:
    from ..models import DeviceEnrolleeSync

    if full:
        DeviceEnrolleeSync.objects.filter(enrollee=enrollee).update(
            user_synced=False, synced_templates={}, status=SyncStatus.PENDING)
    return sync_enrollee(enrollee, force=full, created_by=created_by)


def resync_all(*, full: bool = False) -> dict[str, int]:
    from ..constants import DeviceStatus
    from ..models import Device

    totals = {"devices": 0, "queued": 0}
    for device in Device.objects.filter(status=DeviceStatus.ACTIVE):
        result = resync_device(device, full=full)
        totals["devices"] += 1
        totals["queued"] += result["queued"]
    return totals


# --------------------------------------------------------------------------- acknowledgements


def mark_present(device: Any, enrollee: Any, *, template: Any = None, user: bool = False) -> Any:
    """The device reported holding the user/template (walk-up enrollment, query results)."""
    record = _record(device, enrollee)
    present = dict(record.synced_templates or {})
    if template is not None:
        present[template_key(template.finger_index, template.algorithm_version)] = {
            "version": template.version, "checksum": template.checksum}
    if user or template is not None:
        record.user_synced = True
        present.setdefault(USER_KEY, {"hash": user_payload(enrollee)["user_hash"]})
    record.synced_templates = present
    record.save(update_fields=["synced_templates", "user_synced", "updated_at"])
    _refresh_status(record)
    return record


def _expected_keys(record: Any) -> set[str]:
    templates = list(record.enrollee.templates.all())
    return {template_key(t.finger_index, t.algorithm_version)
            for t in choose_templates(record.device, templates)}


def _refresh_status(record: Any) -> None:
    previous = record.status
    enrollee = record.enrollee
    if not enrollee.is_active:
        return
    if _has_open_sync_commands(record.device, enrollee):
        status = SyncStatus.PENDING
    else:
        present = {k for k in (record.synced_templates or {}) if k != USER_KEY}
        expected = _expected_keys(record)
        if expected <= present and (record.user_synced or not expected):
            status = SyncStatus.SYNCED
        else:
            status = SyncStatus.PARTIAL
    record.status = status
    if status == SyncStatus.SYNCED:
        record.last_synced_at = now()
        record.last_error = ""
    record.save(update_fields=["status", "last_synced_at", "last_error", "updated_at"])
    if status == SyncStatus.SYNCED and previous != SyncStatus.SYNCED:
        emit("sync_completed", {"device": payloads.device_payload(record.device),
                                "enrollee": payloads.enrollee_payload(enrollee)},
             device=record.device, enrollee=enrollee)


def on_command_succeeded(command: Any) -> None:
    enrollee = command.enrollee
    if enrollee is None:
        return
    record = _record(command.device, enrollee)
    present = dict(record.synced_templates or {})
    payload = command.payload or {}
    ctype = command.command_type
    if ctype == "add_user":
        record.user_synced = True
        present[USER_KEY] = {"hash": payload.get("user_hash")}
    elif ctype == "add_template":
        key = payload.get("key")
        if key:
            finger = key.split(":", 1)[0]
            for other in [k for k in present if k.split(":", 1)[0] == finger and k != key]:
                present.pop(other)
            present[key] = {"version": payload.get("version"), "checksum": payload.get("checksum")}
    elif ctype == "delete_template":
        finger = str(payload.get("finger_index"))
        for other in [k for k in present if k.split(":", 1)[0] == finger]:
            present.pop(other)
    elif ctype == "delete_user":
        record.user_synced = False
        record.synced_templates = {}
        in_scope = enrollee.is_active and get_sync_strategy().devices_for(enrollee).filter(
            pk=command.device_id).exists()
        record.status = SyncStatus.OUT_OF_SCOPE if (enrollee.is_active and not in_scope) \
            else SyncStatus.DELETED
        record.last_synced_at = now()
        record.save()
        return
    record.synced_templates = present
    record.save(update_fields=["user_synced", "synced_templates", "updated_at"])
    _refresh_status(record)


def on_command_failed(command: Any) -> None:
    enrollee = command.enrollee
    if enrollee is None:
        return
    record = _record(command.device, enrollee)
    record.status = SyncStatus.FAILED
    record.last_error = (f"{command.command_type} failed (return code {command.return_code}): "
                         f"{command.error}")[:1000]
    record.save(update_fields=["status", "last_error", "updated_at"])
    emit("sync_failed", {"device": payloads.device_payload(command.device),
                         "enrollee": payloads.enrollee_payload(enrollee),
                         "error": record.last_error},
         device=command.device, enrollee=enrollee, error=record.last_error)


# --------------------------------------------------------------------------- reporting


def sync_status_for_enrollee(enrollee: Any) -> list[dict[str, Any]]:
    from ..models import DeviceEnrolleeSync

    return [_record_dict(r) for r in DeviceEnrolleeSync.objects.filter(enrollee=enrollee)
            .select_related("device", "enrollee")]


def sync_status_for_device(device: Any) -> dict[str, Any]:
    from django.db.models import Count

    from ..models import DeviceEnrolleeSync

    records = DeviceEnrolleeSync.objects.filter(device=device)
    counts = dict(records.values_list("status").annotate(n=Count("id")))
    in_scope = get_sync_strategy().enrollees_for(device).count()
    return {
        "device_id": str(device.uuid),
        "enrollees_in_scope": in_scope,
        "status_counts": counts,
        "synced": counts.get(SyncStatus.SYNCED, 0),
        "missing": max(in_scope - counts.get(SyncStatus.SYNCED, 0), 0),
        "records": [_record_dict(r) for r in records.select_related("device", "enrollee")
                    .exclude(status=SyncStatus.SYNCED)[:500]],
    }


def _record_dict(record: Any) -> dict[str, Any]:
    return {
        "device_id": str(record.device.uuid),
        "device_serial": record.device.serial_number,
        "enrollee_id": str(record.enrollee.uuid),
        "device_pin": record.enrollee.device_pin,
        "status": record.status,
        "user_synced": record.user_synced,
        "templates": sorted(k for k in (record.synced_templates or {}) if k != USER_KEY),
        "last_synced_at": record.last_synced_at.isoformat() if record.last_synced_at else None,
        "last_error": record.last_error,
    }
