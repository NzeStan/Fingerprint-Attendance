"""Attendance processor interface.

Set ``ATTENDANCE_PROCESSOR`` to a subclass of :class:`BaseAttendanceProcessor` (or ``None`` to
disable processing entirely and compute attendance yourself from ``Punch`` rows).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..conf import settings
from ..utils.timeutils import daterange
from .boundaries import BaseDayBoundary, get_day_boundary


@dataclass
class EffectivePunch:
    """A punch after adjustments are applied (what processors should use)."""

    punch_id: int
    punched_at: datetime
    state: str
    source: str
    flags: list[str]


@dataclass
class StatusContext:
    """Passed to every ``STATUS_RULES`` callable."""

    enrollee: Any
    work_date: date
    day_info: Any
    shift: Any
    punches: list[EffectivePunch]
    first_in: datetime | None
    last_out: datetime | None
    worked: timedelta | None
    zone: ZoneInfo
    statuses: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    #: set to True in a rule to stop evaluating later rules
    stop: bool = False

    @property
    def has_punches(self) -> bool:
        return bool(self.punches)


class BaseAttendanceProcessor:
    version = "1"

    def __init__(self, boundary: BaseDayBoundary | None = None) -> None:
        self.boundary = boundary or get_day_boundary()

    # -- to implement ----------------------------------------------------------------------
    def process_day(self, enrollee: Any, work_date: date, *, create_empty: bool = False) -> Any:
        """Compute and persist one AttendanceDay (return it, or ``None`` if nothing stored)."""
        raise NotImplementedError

    # -- provided --------------------------------------------------------------------------
    def work_date_for(self, punch: Any) -> date:
        return self.boundary.work_date(punch.punched_at, punch.enrollee)

    def process_punch(self, punch: Any) -> Any:
        if not punch.enrollee_id:
            return None
        return self.process_day(punch.enrollee, self.work_date_for(punch))

    def process_pairs(self, pairs: Iterable[tuple[Any, date]]) -> int:
        from ..models import Enrollee

        grouped: dict[Any, set[date]] = defaultdict(set)
        for enrollee_id, day in pairs:
            grouped[enrollee_id].add(day)
        count = 0
        for enrollee in Enrollee.objects.filter(pk__in=list(grouped)):
            for day in sorted(grouped[enrollee.pk]):
                if self.process_day(enrollee, day) is not None:
                    count += 1
        return count

    def process_range(self, enrollee: Any, start: date, end: date, *,
                      create_empty: bool = False) -> list[Any]:
        days = []
        for day in daterange(start, end):
            result = self.process_day(enrollee, day, create_empty=create_empty)
            if result is not None:
                days.append(result)
        return days

    def recompute(self, *, start: date, end: date, enrollees: Iterable[Any] | None = None) -> int:
        from ..models import AttendanceDay, Enrollee, Punch

        if enrollees is None:
            window_start = self.boundary.window(start)[0] - timedelta(days=1)
            window_end = self.boundary.window(end)[1] + timedelta(days=1)
            ids = set(Punch.objects.filter(punched_at__gte=window_start,
                                           punched_at__lt=window_end, enrollee__isnull=False)
                      .values_list("enrollee_id", flat=True))
            ids |= set(AttendanceDay.objects.filter(work_date__range=(start, end))
                       .values_list("enrollee_id", flat=True))
            enrollees = Enrollee.objects.filter(pk__in=ids)
        count = 0
        for enrollee in enrollees:
            count += len(self.process_range(enrollee, start, end))
        return count


def get_processor() -> BaseAttendanceProcessor | None:
    if not settings.ATTENDANCE_PROCESSOR:
        return None
    obj = settings.import_("ATTENDANCE_PROCESSOR")
    return obj() if isinstance(obj, type) else obj
