"""Day boundaries: which work date a punch belongs to, and which instants make up a work date.

``work_date(t)`` and ``window(d)`` are consistent: a punch at ``t`` belongs to ``d`` exactly
when ``window(d)[0] <= t < window(d)[1]``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..conf import settings
from ..utils.timeutils import get_zone


def attendance_zone() -> ZoneInfo:
    return get_zone(settings.ATTENDANCE_TIMEZONE or settings.DEFAULT_DEVICE_TIMEZONE or "UTC")


def _local_midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=zone)


class BaseDayBoundary:
    def __init__(self, zone: ZoneInfo | None = None) -> None:
        self.zone = zone or attendance_zone()

    def work_date(self, instant: datetime, enrollee: Any = None) -> date:
        raise NotImplementedError

    def window(self, work_date: date, enrollee: Any = None) -> tuple[datetime, datetime]:
        raise NotImplementedError


class CalendarDayBoundary(BaseDayBoundary):
    """Local midnight to midnight."""

    def work_date(self, instant: datetime, enrollee: Any = None) -> date:
        return instant.astimezone(self.zone).date()

    def window(self, work_date: date, enrollee: Any = None) -> tuple[datetime, datetime]:
        return (_local_midnight(work_date, self.zone),
                _local_midnight(work_date + timedelta(days=1), self.zone))


class OffsetDayBoundary(BaseDayBoundary):
    """Work days start at ``DAY_START_TIME`` (e.g. 04:00 so early-morning punches count for
    the previous day)."""

    def __init__(self, zone: ZoneInfo | None = None, start: time | None = None) -> None:
        super().__init__(zone)
        self.start = start or settings.DAY_START_TIME
        self.offset = timedelta(hours=self.start.hour, minutes=self.start.minute,
                                seconds=self.start.second)

    def work_date(self, instant: datetime, enrollee: Any = None) -> date:
        local = instant.astimezone(self.zone).replace(tzinfo=None)
        return (local - self.offset).date()

    def window(self, work_date: date, enrollee: Any = None) -> tuple[datetime, datetime]:
        start = datetime.combine(work_date, self.start, tzinfo=self.zone)
        end = datetime.combine(work_date + timedelta(days=1), self.start, tzinfo=self.zone)
        return start, end


class ShiftAwareDayBoundary(BaseDayBoundary):
    """Uses the schedule provider: punches shortly after an overnight shift ends
    (``OVERNIGHT_SHIFT_MARGIN``) belong to the date the shift started."""

    def __init__(self, zone: ZoneInfo | None = None, margin: timedelta | None = None) -> None:
        super().__init__(zone)
        from ..calendar.providers import get_schedule_provider

        self.schedules = get_schedule_provider()
        self.margin = margin if margin is not None else settings.OVERNIGHT_SHIFT_MARGIN
        self._cache: dict[tuple[Any, date], Any] = {}

    def _shift(self, enrollee: Any, day: date) -> Any:
        if enrollee is None:
            return None
        key = (enrollee.pk, day)
        if key not in self._cache:
            self._cache[key] = self.schedules.get_expected_shift(enrollee, day)
        return self._cache[key]

    def _overnight_end(self, enrollee: Any, day: date) -> datetime | None:
        shift = self._shift(enrollee, day)
        if shift is None or not shift.crosses_midnight:
            return None
        return shift.end_datetime(day, self.zone) + self.margin

    def window(self, work_date: date, enrollee: Any = None) -> tuple[datetime, datetime]:
        start = _local_midnight(work_date, self.zone)
        prev_end = self._overnight_end(enrollee, work_date - timedelta(days=1))
        if prev_end is not None and prev_end > start:
            start = prev_end
        end = self._overnight_end(enrollee, work_date) or _local_midnight(
            work_date + timedelta(days=1), self.zone)
        return start, end

    def work_date(self, instant: datetime, enrollee: Any = None) -> date:
        local_date = instant.astimezone(self.zone).date()
        prev_end = self._overnight_end(enrollee, local_date - timedelta(days=1))
        if prev_end is not None and instant < prev_end:
            return local_date - timedelta(days=1)
        return local_date


def get_day_boundary() -> BaseDayBoundary:
    obj = settings.import_("DAY_BOUNDARY_RESOLVER")
    return obj() if isinstance(obj, type) else obj
