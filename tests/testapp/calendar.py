"""Consumer-style calendar extensions used by the tests."""

from __future__ import annotations

from datetime import date
from typing import Any

from fingerprint_attendance.calendar.providers import BaseLeaveProvider, DefaultCalendarProvider
from fingerprint_attendance.calendar.types import DayInfo, LeaveInfo
from fingerprint_attendance.registry import attendance_statuses, day_types

day_types.register("company_event", "Company event", is_working=True)
attendance_statuses.register("attended_event", "Attended company event")

LEAVES: dict[tuple[int, date], LeaveInfo] = {}
EVENTS: set[date] = set()


class DictLeaveProvider(BaseLeaveProvider):
    def get_leaves(self, enrollee: Any, start: date, end: date) -> dict[date, LeaveInfo]:
        return {d: info for (pk, d), info in LEAVES.items()
                if pk == enrollee.pk and start <= d <= end}


class EventCalendarProvider(DefaultCalendarProvider):
    def classify(self, enrollee: Any, day: date, **kwargs: Any) -> DayInfo:
        if day in EVENTS:
            return DayInfo(day, "company_event", "Annual retreat", True, {})
        return super().classify(enrollee, day, **kwargs)


def event_rule(ctx: Any) -> str | None:
    if ctx.day_info.day_type == "company_event" and ctx.has_punches:
        ctx.stop = True
        return "attended_event"
    return None
