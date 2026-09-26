"""JSON-serializable event payloads. Template bytes and secrets never appear here."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


def _dt(value: datetime | date | None) -> str | None:
    return value.isoformat() if value else None


def device_payload(device: Any) -> dict[str, Any]:
    return {
        "id": str(device.uuid),
        "serial_number": device.serial_number,
        "name": device.name,
        "status": device.status,
        "is_online": device.is_online,
        "last_seen_at": _dt(device.last_seen_at),
        "location": device.location,
    }


def enrollee_payload(enrollee: Any) -> dict[str, Any]:
    return {
        "id": str(enrollee.uuid),
        "device_pin": enrollee.device_pin,
        "display_name": enrollee.display_name,
        "employee_id": str(enrollee.employee_id),
        "is_active": enrollee.is_active,
    }


def punch_payload(punch: Any) -> dict[str, Any]:
    return {
        "id": str(punch.uuid),
        "sequence": punch.pk,
        "enrollee_id": str(punch.enrollee.uuid) if punch.enrollee_id and punch.enrollee else None,
        "employee_id": str(punch.enrollee.employee_id)
        if punch.enrollee_id and punch.enrollee else None,
        "raw_pin": punch.raw_pin,
        "device_id": str(punch.device.uuid) if punch.device_id and punch.device else None,
        "device_serial": punch.device.serial_number if punch.device_id and punch.device else None,
        "punched_at": _dt(punch.punched_at),
        "received_at": _dt(punch.received_at),
        "state": punch.state,
        "verify_mode": punch.verify_mode,
        "source": punch.source,
        "flags": punch.flag_list,
    }


def command_payload(command: Any) -> dict[str, Any]:
    return {
        "id": str(command.uuid),
        "command_id": command.pk,
        "device_id": str(command.device.uuid),
        "device_serial": command.device.serial_number,
        "command_type": command.command_type,
        "status": command.status,
        "attempts": command.attempts,
        "return_code": command.return_code,
        "correlation_id": command.correlation_id,
    }


def session_payload(session: Any) -> dict[str, Any]:
    return {
        "id": str(session.uuid),
        "enrollee_id": str(session.enrollee.uuid),
        "device_id": str(session.device.uuid) if session.device_id else None,
        "agent_id": str(session.agent.uuid) if session.agent_id else None,
        "status": session.status,
        "fingers_requested": session.fingers_requested,
        "fingers_captured": session.fingers_captured,
        "expires_at": _dt(session.expires_at),
    }


def template_payload(template: Any) -> dict[str, Any]:
    return {
        "id": str(template.uuid),
        "enrollee_id": str(template.enrollee.uuid),
        "finger_index": template.finger_index,
        "algorithm_version": template.algorithm_version,
        "version": template.version,
        "checksum": template.checksum,
        "source": template.source,
        "quality": template.quality,
    }


def consent_payload(consent: Any) -> dict[str, Any]:
    return {
        "id": str(consent.uuid),
        "enrollee_id": str(consent.enrollee.uuid),
        "version": consent.version,
        "given_at": _dt(consent.given_at),
        "withdrawn_at": _dt(consent.withdrawn_at),
        "method": consent.method,
    }


def attendance_day_payload(day: Any) -> dict[str, Any]:
    worked: timedelta | None = day.worked_duration
    return {
        "id": str(day.uuid),
        "enrollee_id": str(day.enrollee.uuid),
        "work_date": _dt(day.work_date),
        "status": day.status,
        "statuses": day.statuses,
        "first_in": _dt(day.first_in),
        "last_out": _dt(day.last_out),
        "worked_seconds": worked.total_seconds() if worked is not None else None,
        "day_type": day.day_type,
    }
