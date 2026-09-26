"""Punch entry points besides devices: manual punches, bulk import, adjustments."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ..exceptions import InvalidInput
from ..ingestion.candidates import PunchCandidate
from ..ingestion.pipeline import IngestResult, ingest_punches, schedule_processing
from ..registry import punch_sources, punch_states
from ..utils.timeutils import ensure_aware
from .audit import log_action


def create_manual_punch(enrollee: Any, punched_at: datetime | str, *, state: str = "",
                        device: Any = None, by: Any = None, note: str = "") -> Any:
    """A permission-gated manual punch, always flagged ``manual``."""
    if state and state not in punch_states:
        raise InvalidInput(f"unknown punch state {state!r}")
    when = ensure_aware(punched_at)
    candidate = PunchCandidate(pin=enrollee.device_pin, punched_at=when, source="manual",
                               device=device, state=state or None, created_by=by, note=note,
                               raw_payload="manual", enrollee=enrollee, flags={"manual"})
    result = ingest_punches([candidate], device=device)
    if not result.created:
        raise InvalidInput("an identical punch already exists", code="duplicate_punch")
    punch = result.created[0]
    log_action("punch.manual_create", actor=by, obj=enrollee,
               metadata={"punch": str(punch.uuid), "punched_at": when.isoformat(),
                         "note": note})
    return punch


def import_punches(rows: Iterable[dict[str, Any]], *, source: str = "import",
                   by: Any = None) -> IngestResult:
    """Bulk import. Each row: ``pin``, ``punched_at`` (ISO; naive = device/default timezone),
    optional ``device_serial``, ``state`` (raw device code), ``verify_mode``, ``work_code``."""
    from ..models import Device

    if source not in punch_sources:
        raise InvalidInput(f"unknown punch source {source!r}")
    rows = list(rows)
    serials = {str(r["device_serial"]) for r in rows if r.get("device_serial")}
    devices = {d.serial_number: d for d in Device.objects.filter(serial_number__in=serials)}
    candidates = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        try:
            device = devices.get(str(row.get("device_serial") or ""))
            if row.get("device_serial") and device is None:
                raise ValueError(f"unknown device {row['device_serial']}")
            from ..utils.timeutils import device_zone

            when = ensure_aware(row["punched_at"], device_zone(device))
            candidates.append(PunchCandidate(
                pin=str(row["pin"]), punched_at=when, source=source, device=device,
                raw_state=str(row.get("state", "") or ""),
                verify_mode=int(row["verify_mode"]) if row.get("verify_mode") not in (None, "")
                else None,
                work_code=str(row.get("work_code", "") or ""), raw_payload=f"import:{index}",
                created_by=by))
        except (KeyError, ValueError, TypeError) as exc:
            errors.append({"row": index, "error": str(exc)})
    result = ingest_punches(candidates)
    log_action("punch.import", actor=by, metadata={"rows": len(rows),
                                                   "created": result.created_count,
                                                   "duplicates": result.duplicates,
                                                   "errors": len(errors)})
    result.rejected.extend((PunchCandidate(pin=str(e["row"])), e["error"]) for e in errors)
    return result


def adjust_punch(punch: Any, *, action: str, new_state: str = "",
                 new_punched_at: datetime | str | None = None, reason: str = "",
                 by: Any = None) -> Any:
    """Record a correction; the raw punch is never modified."""
    from ..models import PunchAdjustment
    from ..processing.boundaries import get_day_boundary

    if action not in PunchAdjustment.Action.values:
        raise InvalidInput(f"unknown adjustment action {action!r}")
    if action == PunchAdjustment.Action.SET_STATE and new_state not in punch_states:
        raise InvalidInput("a valid new_state is required")
    when = ensure_aware(new_punched_at) if new_punched_at else None
    if action == PunchAdjustment.Action.SET_TIME and when is None:
        raise InvalidInput("new_punched_at is required")
    adjustment = PunchAdjustment.objects.create(
        punch=punch, action=action, new_state=new_state, new_punched_at=when, reason=reason,
        created_by=by if getattr(by, "pk", None) else None)
    log_action("punch.adjust", actor=by, obj=punch.enrollee or punch,
               metadata={"punch": str(punch.uuid), "action": action, "reason": reason})
    if punch.enrollee_id:
        boundary = get_day_boundary()
        pairs = {(punch.enrollee_id, boundary.work_date(punch.punched_at, punch.enrollee))}
        if when is not None:
            pairs.add((punch.enrollee_id, boundary.work_date(when, punch.enrollee)))
        schedule_processing(pairs)
    return adjustment
