"""Enrollee lifecycle: create, update, activate/deactivate, delete."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import transaction

from .. import payloads
from ..conf import import_object, settings
from ..constants import OPEN_SESSION_STATUSES, SessionStatus
from ..events import emit
from ..exceptions import InvalidInput
from ..pins import generate_pin, validate_pin
from ..utils.timeutils import now
from .audit import log_action

DEVICE_NAME_MAX = 24


def employee_display_name(employee: Any) -> str:
    field = settings.EMPLOYEE_DISPLAY_FIELD
    if field:
        if "." in field and "__" not in field:
            return str(import_object(field, setting="EMPLOYEE_DISPLAY_FIELD")(employee))
        value: Any = employee
        for part in field.split("__"):
            value = getattr(value, part, "")
            if callable(value):
                value = value()
        return str(value or "")
    getter = getattr(employee, "get_full_name", None)
    if callable(getter):
        name = getter()
        if name:
            return str(name)
    return str(employee)


def device_name_for(enrollee: Any) -> str:
    """Name sent to devices (ASCII-safe fields are the device's job; we only trim)."""
    name = enrollee.display_name or employee_display_name(enrollee.employee)
    return name.replace("\t", " ").replace("\n", " ")[:DEVICE_NAME_MAX]


def create_enrollee(employee: Any, *, device_pin: str | None = None,
                    display_name: str | None = None, privilege: int = 0,
                    groups: Iterable[Any] = (), by: Any = None,
                    metadata: dict[str, Any] | None = None) -> Any:
    from ..models import Enrollee

    if Enrollee.objects.filter(employee=employee).exists():
        raise InvalidInput("this employee is already enrolled", code="already_enrolled")
    with transaction.atomic():
        pin = validate_pin(device_pin if device_pin else generate_pin(employee))
        enrollee = Enrollee.objects.create(
            employee=employee, device_pin=pin,
            display_name=(display_name if display_name is not None
                          else employee_display_name(employee))[:64],
            privilege=privilege, metadata=metadata or {})
        if groups:
            enrollee.groups.set(list(groups))
        log_action("enrollee.create", actor=by, obj=enrollee)
    if settings.LINK_UNKNOWN_PUNCHES_ON_ENROLL:
        link_unknown_punches(enrollee)
    if settings.SYNC_USERS_WITHOUT_TEMPLATES:
        from ..sync.engine import sync_enrollee

        sync_enrollee(enrollee)
    return enrollee


def update_enrollee(enrollee: Any, *, by: Any = None, groups: Iterable[Any] | None = None,
                    **changes: Any) -> Any:
    allowed = {"display_name", "privilege", "metadata", "device_pin"}
    unknown = set(changes) - allowed
    if unknown:
        raise InvalidInput(f"cannot update {sorted(unknown)}")
    resync = False
    with transaction.atomic():
        if "device_pin" in changes and changes["device_pin"] != enrollee.device_pin:
            old_pin = enrollee.device_pin
            new_pin = validate_pin(changes.pop("device_pin"), exclude_enrollee=enrollee)
            from ..sync.engine import remove_enrollee_from_devices

            remove_enrollee_from_devices(enrollee, reason="pin changed")
            # devices will hold nothing under the new PIN: push everything again
            from ..models import DeviceEnrolleeSync

            DeviceEnrolleeSync.objects.filter(enrollee=enrollee).update(
                user_synced=False, synced_templates={})
            _retire_pin(old_pin, enrollee)
            enrollee.device_pin = new_pin
            resync = True
        else:
            changes.pop("device_pin", None)
        for key, value in changes.items():
            if getattr(enrollee, key) != value:
                setattr(enrollee, key, value)
                resync = resync or key in ("display_name", "privilege")
        enrollee.save()
        if groups is not None:
            enrollee.groups.set(list(groups))
            resync = True
        log_action("enrollee.update", actor=by, obj=enrollee,
                   metadata={"fields": sorted(changes)})
    if resync and enrollee.is_active:
        from ..sync.engine import sync_enrollee

        sync_enrollee(enrollee)
    return enrollee


def deactivate_enrollee(enrollee: Any, *, by: Any = None, reason: str = "") -> Any:
    if not enrollee.is_active:
        return enrollee
    enrollee.is_active = False
    enrollee.deactivated_at = now()
    enrollee.save(update_fields=["is_active", "deactivated_at", "updated_at"])
    cancel_open_sessions(enrollee, reason="enrollee deactivated")
    if settings.SYNC_ON_DELETE:
        from ..sync.engine import remove_enrollee_from_devices

        remove_enrollee_from_devices(enrollee, reason="deactivated", created_by=by)
    log_action("enrollee.deactivate", actor=by, obj=enrollee, metadata={"reason": reason})
    emit("enrollee_deactivated", payloads.enrollee_payload(enrollee), enrollee=enrollee)
    return enrollee


def activate_enrollee(enrollee: Any, *, by: Any = None) -> Any:
    if enrollee.is_active:
        return enrollee
    enrollee.is_active = True
    enrollee.deactivated_at = None
    enrollee.save(update_fields=["is_active", "deactivated_at", "updated_at"])
    log_action("enrollee.activate", actor=by, obj=enrollee)
    emit("enrollee_activated", payloads.enrollee_payload(enrollee), enrollee=enrollee)
    from ..sync.engine import sync_enrollee

    sync_enrollee(enrollee)
    return enrollee


def delete_enrollee(enrollee: Any, *, by: Any = None) -> None:
    """Delete the enrollee and their templates; device deletes are queued first (see the
    ``pre_delete`` receiver, which also covers ORM/cascade deletes)."""
    log_action("enrollee.delete", actor=by, obj=enrollee,
               metadata={"device_pin": enrollee.device_pin})
    enrollee._fpa_deleted_by = by
    enrollee.delete()


def handle_enrollee_pre_delete(enrollee: Any) -> None:
    """Called by the ``pre_delete`` receiver."""
    by = getattr(enrollee, "_fpa_deleted_by", None)
    cancel_open_sessions(enrollee, reason="enrollee deleted")
    if settings.SYNC_ON_DELETE:
        from ..sync.engine import remove_enrollee_from_devices

        remove_enrollee_from_devices(enrollee, reason="deleted", everywhere=True, created_by=by)
    _retire_pin(enrollee.device_pin, enrollee)


def _retire_pin(pin: str, enrollee: Any) -> None:
    from ..models import RetiredPin

    if settings.PIN_REUSE_ALLOWED:
        return
    RetiredPin.objects.get_or_create(pin=pin, defaults={"enrollee_uuid": enrollee.uuid})


def cancel_open_sessions(enrollee: Any, *, reason: str) -> int:
    from ..models import EnrollmentSession
    from .enrollment import cancel_session

    count = 0
    for session in EnrollmentSession.objects.filter(enrollee=enrollee,
                                                    status__in=OPEN_SESSION_STATUSES):
        cancel_session(session, reason=reason, status=SessionStatus.CANCELLED)
        count += 1
    return count


def link_unknown_punches(enrollee: Any) -> int:
    """Attach earlier unknown-PIN punches with this PIN (raw fields are untouched)."""
    from ..models import Punch
    from ..processing.boundaries import get_day_boundary

    qs = Punch.objects.filter(raw_pin=enrollee.device_pin, enrollee__isnull=True)
    # never attach punches from before a previous holder's time if PINs are reused
    if enrollee.created_at and settings.PIN_REUSE_ALLOWED:
        qs = qs.filter(punched_at__gte=enrollee.created_at)
    punches = list(qs.values_list("punched_at", flat=True))
    updated = qs.update(enrollee=enrollee)
    if updated:
        from ..ingestion.pipeline import schedule_processing

        boundary = get_day_boundary()
        schedule_processing({(enrollee.pk, boundary.work_date(p, enrollee)) for p in punches})
    return updated
