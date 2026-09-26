"""The punch ingestion pipeline, shared by ADMS, pull mode, imports and manual entry.

1. normalise to UTC with the device timezone
2. resolve the enrollee by PIN
3. build the dedupe key (``DEDUPE_KEY_BUILDER``); drop exact duplicates
4. detect soft duplicates inside ``DEDUPE_WINDOW_SECONDS`` (flag or skip)
5. flag anomalies: future, clock drift, unknown PIN, late sync
6. resolve the punch state (``PUNCH_STATE_RESOLVER``)
7. save atomically with ``bulk_create(ignore_conflicts=True)`` in chunks
8. emit signals / hooks / webhooks / realtime
9. queue attendance processing for every affected (enrollee, work date)
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.db import transaction

from .. import payloads
from ..conf import settings
from ..events import emit
from ..models import Enrollee, Punch, encode_flags
from ..processing.boundaries import get_day_boundary
from ..utils.logging import get_logger
from ..utils.timeutils import device_zone, now, to_utc
from .candidates import PunchCandidate
from .dedupe import hash_key
from .state import ResolverContext, get_state_resolver

logger = get_logger(__name__)


@dataclass
class IngestResult:
    created: list[Any] = field(default_factory=list)
    duplicates: int = 0
    soft_duplicates: int = 0
    rejected: list[tuple[PunchCandidate, str]] = field(default_factory=list)
    affected: set[tuple[int, date]] = field(default_factory=set)
    batch_id: uuid.UUID | None = None

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def date_range(self) -> tuple[date, date] | None:
        dates = [d for _, d in self.affected]
        return (min(dates), max(dates)) if dates else None


def ingest_punches(candidates: Iterable[PunchCandidate], *, device: Any = None,
                   received_at: datetime | None = None, emit_events: bool = True,
                   process: bool = True) -> IngestResult:
    """Run candidates through the pipeline and return what happened."""
    result = IngestResult(batch_id=uuid.uuid4())
    received = received_at or now()
    items = list(candidates)
    if not items:
        return result
    boundary = get_day_boundary()

    # 1. normalise -------------------------------------------------------------------
    valid: list[PunchCandidate] = []
    for c in items:
        if c.device is None:
            c.device = device
        if c.punched_at is None:
            if c.local_time is None:
                result.rejected.append((c, "no time"))
                continue
            c.punched_at = to_utc(c.local_time, device_zone(c.device))
        elif c.punched_at.tzinfo is None:
            c.punched_at = to_utc(c.punched_at, device_zone(c.device))
        if not c.raw_time:
            c.raw_time = (c.local_time.strftime("%Y-%m-%d %H:%M:%S") if c.local_time else "")
        c.pin = str(c.pin).strip()
        valid.append(c)

    # 2. enrollees ---------------------------------------------------------------------
    pins = {c.pin for c in valid}
    enrollees = {e.device_pin: e for e in Enrollee.objects.filter(device_pin__in=pins)}
    kept: list[PunchCandidate] = []
    for c in valid:
        c.enrollee = c.enrollee or enrollees.get(c.pin)
        if c.enrollee is None:
            if not settings.ACCEPT_UNKNOWN_PIN_PUNCHES:
                result.rejected.append((c, "unknown pin"))
                continue
            c.flags.add("unknown_pin")
        kept.append(c)

    # 3. exact duplicates -------------------------------------------------------------------
    key_builder = settings.import_("DEDUPE_KEY_BUILDER")
    unique: dict[str, PunchCandidate] = {}
    for c in kept:
        c.dedupe_hash = hash_key(key_builder(c))
        if c.dedupe_hash in unique:
            result.duplicates += 1
            continue
        unique[c.dedupe_hash] = c
    existing = set(Punch.objects.filter(dedupe_hash__in=list(unique))
                   .values_list("dedupe_hash", flat=True))
    result.duplicates += len(existing)
    fresh = sorted((c for h, c in unique.items() if h not in existing),
                   key=lambda c: (c.punched_at, c.pin))
    if not fresh:
        return result

    for c in fresh:
        assert c.punched_at is not None
        c.work_date = boundary.work_date(c.punched_at, c.enrollee)

    # 4. soft duplicates + history for sequence resolvers ------------------------------------
    window = timedelta(seconds=max(int(settings.DEDUPE_WINDOW_SECONDS or 0), 0))
    resolver = get_state_resolver()
    history = _load_history(fresh, boundary, window, need_days=resolver.needs_history)
    if window:
        fresh = _mark_soft_duplicates(fresh, history["times"], window, result)

    # 5. anomalies ---------------------------------------------------------------------------
    tolerance = settings.FUTURE_PUNCH_TOLERANCE
    late = settings.LATE_SYNC_THRESHOLD
    drift_limit = settings.CLOCK_DRIFT_WARNING_SECONDS
    for c in fresh:
        assert c.punched_at is not None
        if c.punched_at > received + tolerance:
            c.flags.add("future")
        if received - c.punched_at > late and c.source not in ("manual", "import"):
            c.flags.add("late_sync")
        drift = getattr(c.device, "clock_drift_seconds", None) if c.device is not None else None
        if drift is not None and abs(drift) > drift_limit:
            c.flags.add("clock_drift")

    # 6. state -------------------------------------------------------------------------------
    to_resolve = [c for c in fresh if not c.state and "duplicate" not in c.flags]
    resolver.resolve(to_resolve, ResolverContext(history["counts"]))
    for c in fresh:
        c.state = c.state or "unknown"

    # 7. save --------------------------------------------------------------------------------
    rows = [
        Punch(
            enrollee=c.enrollee,
            raw_pin=c.pin[:24],
            device=c.device,
            punched_at=c.punched_at,  # type: ignore[misc]  # set in step 1
            device_local_time=c.raw_time[:32],
            received_at=received,
            verify_mode=c.verify_mode,
            raw_state=str(c.raw_state or "")[:8],
            state=c.state or "unknown",
            work_code=str(c.work_code or "")[:32],
            raw_payload=c.raw_payload[:2000],
            dedupe_hash=c.dedupe_hash,
            source=c.source,
            flags=encode_flags(c.flags),
            batch_id=result.batch_id,
            created_by=c.created_by if getattr(c.created_by, "pk", None) else None,
            note=c.note,
        )
        for c in fresh
    ]
    with transaction.atomic():
        Punch.objects.bulk_create(rows, batch_size=settings.PUNCH_BULK_CHUNK_SIZE,
                                  ignore_conflicts=True)
        created = list(Punch.objects.filter(batch_id=result.batch_id)
                       .select_related("enrollee", "device").order_by("punched_at", "id"))
        result.duplicates += len(rows) - len(created)
        result.created = created
        _update_last_punch(created)

    by_hash = {c.dedupe_hash: c for c in fresh}
    for punch in created:
        cand = by_hash.get(punch.dedupe_hash)
        if punch.enrollee_id and cand is not None and cand.work_date is not None \
                and not punch.has_flag("duplicate"):
            result.affected.add((punch.enrollee_id, cand.work_date))

    # 8. events ------------------------------------------------------------------------------
    if emit_events and created:
        _emit_events(result, device)
    # 9. processing --------------------------------------------------------------------------
    if process and result.affected:
        schedule_processing(result.affected)
    return result


def _load_history(fresh: list[PunchCandidate], boundary: Any, window: timedelta, *,
                  need_days: bool) -> dict[str, Any]:
    """One query: existing effective punches for these PINs around the batch."""
    counts: dict[tuple[str, date], int] = defaultdict(int)
    times: dict[str, list[datetime]] = defaultdict(list)
    if not window and not need_days:
        return {"counts": counts, "times": times}
    pins = {c.pin for c in fresh}
    enrollee_by_pin = {c.pin: c.enrollee for c in fresh}
    starts, ends = [], []
    for c in fresh:
        assert c.work_date is not None
        assert c.punched_at is not None
        if need_days:
            start, end = boundary.window(c.work_date, c.enrollee)
            starts.append(start)
            ends.append(end)
        starts.append(c.punched_at - window)
        ends.append(c.punched_at + window)
    qs = (Punch.objects.effective().filter(raw_pin__in=pins,
                                           punched_at__gte=min(starts),
                                           punched_at__lte=max(ends))
          .values_list("raw_pin", "punched_at"))
    for pin, punched_at in qs:
        times[pin].append(punched_at)
        if need_days:
            counts[(pin, boundary.work_date(punched_at, enrollee_by_pin.get(pin)))] += 1
    return {"counts": counts, "times": times}


def _mark_soft_duplicates(fresh: list[PunchCandidate], existing: dict[str, list[datetime]],
                          window: timedelta, result: IngestResult) -> list[PunchCandidate]:
    action = settings.DEDUPE_WINDOW_ACTION or "flag"
    accepted: dict[str, list[datetime]] = defaultdict(list)
    out: list[PunchCandidate] = []
    for c in fresh:
        assert c.punched_at is not None
        near = any(abs(c.punched_at - t) <= window
                   for t in existing.get(c.pin, []) + accepted[c.pin])
        if near:
            result.soft_duplicates += 1
            if action == "skip":
                continue
            c.flags.add("duplicate")
        else:
            accepted[c.pin].append(c.punched_at)
        out.append(c)
    return out


def _update_last_punch(created: list[Any]) -> None:
    latest: dict[int, datetime] = {}
    for p in created:
        if p.device_id and (p.device_id not in latest or p.punched_at > latest[p.device_id]):
            latest[p.device_id] = p.punched_at
    from django.db.models import Q

    from ..models import Device

    for device_id, when in latest.items():
        Device.objects.filter(pk=device_id).filter(
            Q(last_punch_at__isnull=True) | Q(last_punch_at__lt=when)).update(last_punch_at=when)


def _emit_events(result: IngestResult, device: Any) -> None:
    created = result.created
    limit = settings.PUNCH_EVENT_BATCH_LIMIT
    per_punch = limit is None or len(created) <= limit
    if per_punch:
        for punch in created:
            body = payloads.punch_payload(punch)
            emit("punch_received", body, punch=punch)
            interesting = [f for f in punch.flag_list if f not in ("manual",)]
            if interesting:
                emit("punch_flagged", {**body, "flags": interesting}, punch=punch,
                     flags=interesting)
    dev = device or (created[0].device if created else None)
    date_range = result.date_range
    batch_body = {
        "count": len(created),
        "duplicates": result.duplicates,
        "soft_duplicates": result.soft_duplicates,
        "device": payloads.device_payload(dev) if dev is not None else None,
        "first_punch_at": created[0].punched_at.isoformat(),
        "last_punch_at": created[-1].punched_at.isoformat(),
        "batch_id": str(result.batch_id),
    }
    emit("punches_received", batch_body, punches=created, device=dev)
    late = any(p.has_flag("late_sync") for p in created)
    if len(created) >= settings.BACKLOG_SIGNAL_THRESHOLD or late:
        start = date_range[0] if date_range else created[0].punched_at.date()
        end = date_range[1] if date_range else created[-1].punched_at.date()
        emit("backlog_synced",
             {**batch_body, "start_date": start.isoformat(), "end_date": end.isoformat()},
             device=dev, count=len(created), start_date=start, end_date=end)


PROCESS_CHUNK = 500


def schedule_processing(pairs: Iterable[tuple[int, date]]) -> None:
    """Queue attendance processing for (enrollee_pk, work_date) pairs via the task backend."""
    if not settings.ATTENDANCE_PROCESSOR or not settings.PROCESS_ON_INGEST:
        return
    from ..tasks.backends import enqueue

    items = sorted({(int(e), d.isoformat()) for e, d in pairs})
    for i in range(0, len(items), PROCESS_CHUNK):
        enqueue("fingerprint_attendance.tasks.jobs.process_attendance",
                [list(p) for p in items[i:i + PROCESS_CHUNK]])
