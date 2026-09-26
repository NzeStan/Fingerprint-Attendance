"""Attendance processing entry points: recompute, calendar changes, absence generation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from ..conf import settings
from ..events import emit
from ..processing.base import get_processor
from ..processing.boundaries import attendance_zone
from ..utils.logging import get_logger
from ..utils.timeutils import now

logger = get_logger(__name__)


def process_pairs(pairs: Iterable[tuple[Any, Any]]) -> int:
    processor = get_processor()
    if processor is None:
        return 0
    normalised = [(int(e), d if isinstance(d, date) else date.fromisoformat(str(d)))
                  for e, d in pairs]
    return processor.process_pairs(normalised)


def recompute(*, start: date, end: date, enrollee_ids: Iterable[int] | None = None) -> int:
    from ..models import Enrollee

    processor = get_processor()
    if processor is None:
        return 0
    enrollees = Enrollee.objects.filter(pk__in=list(enrollee_ids)) if enrollee_ids is not None \
        else None
    return processor.recompute(start=start, end=end, enrollees=enrollees)


def calendar_changed(start: date, end: date, *, enrollee_ids: Iterable[int] | None = None,
                     reason: str = "") -> None:
    """Tell the package that holidays / leave / schedules changed for a date range.

    Call it from your HR system integration; the DB providers call it automatically.
    """
    ids = sorted({int(i) for i in enrollee_ids}) if enrollee_ids is not None else None
    emit("calendar_changed", {"start_date": start.isoformat(), "end_date": end.isoformat(),
                              "enrollee_ids": ids, "reason": reason},
         start_date=start, end_date=end, enrollee_ids=ids)
    if not settings.RECOMPUTE_ON_CALENDAR_CHANGE or not settings.ATTENDANCE_PROCESSOR:
        return
    from ..tasks.backends import enqueue

    enqueue("fingerprint_attendance.tasks.jobs.recompute_attendance", start.isoformat(),
            end.isoformat(), ids)


def generate_absences(day: date | None = None, *, force: bool = False) -> int:
    """Create/refresh AttendanceDay rows for every active enrollee on ``day`` (default:
    yesterday), so working days without punches become ``absent``."""
    from ..models import Enrollee

    processor = get_processor()
    if processor is None or not settings.ABSENCE_GENERATION_ENABLED:
        return 0
    zone = attendance_zone()
    local_now = now().astimezone(zone)
    if day is None:
        day = local_now.date() - timedelta(days=1)
    cutoff = datetime.combine(day, settings.ABSENCE_CUTOFF_TIME, tzinfo=zone)
    if not force and local_now < cutoff:
        logger.info("Not generating absences for %s before the cut-off", day)
        return 0
    day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    count = 0
    for enrollee in Enrollee.objects.filter(is_active=True, created_at__lt=day_end):
        if processor.process_day(enrollee, day, create_empty=True) is not None:
            count += 1
    return count
