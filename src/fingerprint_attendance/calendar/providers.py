"""Calendar, leave and schedule providers.

The attendance processor only talks to these interfaces; where the data comes from (settings,
database, an HR system) is up to the provider configured in settings.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..conf import settings
from ..utils.timeutils import daterange
from .types import DayInfo, LeaveInfo, ShiftInfo


def _instantiate(name: str) -> Any:
    obj = settings.import_(name)
    return obj() if isinstance(obj, type) else obj


def get_calendar_provider() -> BaseCalendarProvider:
    return _instantiate("CALENDAR_PROVIDER")


def get_holiday_provider() -> Any:
    return _instantiate("HOLIDAY_PROVIDER")


def get_leave_provider() -> BaseLeaveProvider:
    return _instantiate("LEAVE_PROVIDER")


def get_schedule_provider() -> BaseScheduleProvider:
    return _instantiate("SCHEDULE_PROVIDER")


# --------------------------------------------------------------------------- leave


class BaseLeaveProvider:
    def get_leaves(self, enrollee: Any, start: date, end: date) -> dict[date, LeaveInfo]:
        raise NotImplementedError

    def get_leave(self, enrollee: Any, day: date) -> LeaveInfo | None:
        return self.get_leaves(enrollee, day, day).get(day)


class NullLeaveProvider(BaseLeaveProvider):
    """Default: no leave information. Connect your HR/leave system with a subclass."""

    def get_leaves(self, enrollee: Any, start: date, end: date) -> dict[date, LeaveInfo]:
        return {}


# --------------------------------------------------------------------------- schedules


class BaseScheduleProvider:
    def get_expected_shift(self, enrollee: Any, day: date) -> ShiftInfo | None:
        raise NotImplementedError

    def get_range(self, enrollee: Any, start: date, end: date) -> dict[date, ShiftInfo | None]:
        return {d: self.get_expected_shift(enrollee, d) for d in daterange(start, end)}


class SettingsScheduleProvider(BaseScheduleProvider):
    """Fixed schedule from ``DEFAULT_SCHEDULE`` (optionally limited to ``weekdays``)."""

    def get_expected_shift(self, enrollee: Any, day: date) -> ShiftInfo | None:
        config = settings.DEFAULT_SCHEDULE
        if not config:
            return None
        weekdays = config.get("weekdays")
        if weekdays is not None and day.weekday() not in [int(w) for w in weekdays]:
            return None
        return ShiftInfo.from_config(config)


class DatabaseScheduleProvider(BaseScheduleProvider):
    """``ShiftAssignment`` rows (enrollee-specific beats group at equal priority)."""

    def get_expected_shift(self, enrollee: Any, day: date) -> ShiftInfo | None:
        from django.db.models import Q

        from ..models import ShiftAssignment

        group_ids = list(enrollee.groups.values_list("pk", flat=True))
        target = Q(enrollee=enrollee)
        if group_ids:
            target |= Q(device_group_id__in=group_ids)
        candidates = (
            ShiftAssignment.objects.select_related("schedule")
            .filter(target, start_date__lte=day)
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=day))
        )
        best = None
        best_key: tuple[int, int] | None = None
        for assignment in candidates:
            schedule = assignment.schedule
            if schedule.weekdays and day.weekday() not in [int(w) for w in schedule.weekdays]:
                continue
            key = (assignment.priority, 1 if assignment.enrollee_id else 0)
            if best_key is None or key > best_key:
                best, best_key = assignment, key
        if best is None:
            return None
        s = best.schedule
        return ShiftInfo.from_config({
            "start": s.start_time.strftime("%H:%M:%S"),
            "end": s.end_time.strftime("%H:%M:%S"),
            "crosses_midnight": s.crosses_midnight,
            "grace_in_minutes": s.grace_in_minutes,
            "grace_out_minutes": s.grace_out_minutes,
            "break_minutes": s.break_minutes,
            "name": s.name,
            "metadata": {"schedule_id": str(s.uuid), "assignment_id": str(best.uuid)},
        })


# --------------------------------------------------------------------------- calendar


class BaseCalendarProvider:
    def get_day_info(self, enrollee: Any, day: date) -> DayInfo:
        raise NotImplementedError

    def get_day_type(self, enrollee: Any, day: date) -> str:
        return self.get_day_info(enrollee, day).day_type

    def get_range(self, enrollee_or_queryset: Any, start: date,
                  end: date) -> dict[Any, dict[date, DayInfo]]:
        """``{enrollee_pk: {date: DayInfo}}`` for one enrollee or an iterable of enrollees."""
        enrollees = ([enrollee_or_queryset] if hasattr(enrollee_or_queryset, "device_pin")
                     else list(enrollee_or_queryset))
        return {e.pk: {d: self.get_day_info(e, d) for d in daterange(start, end)}
                for e in enrollees}


class DefaultCalendarProvider(BaseCalendarProvider):
    """Resolution order: public holiday > weekend > leave > working day.

    Weekend days come from ``WEEKEND_DAYS`` unless the enrollee belongs to a device group
    listed in ``WEEKEND_DAYS_BY_GROUP``. Subclass and override :meth:`classify` to add custom
    day types (register them in ``fingerprint_attendance.registry.day_types``).
    """

    def __init__(self) -> None:
        self.holidays = get_holiday_provider()
        self.leave = get_leave_provider()

    def weekend_days(self, enrollee: Any) -> set[int]:
        overrides = settings.WEEKEND_DAYS_BY_GROUP or {}
        if overrides and enrollee is not None:
            for name in enrollee.groups.values_list("name", flat=True):
                if name in overrides:
                    return {int(d) for d in overrides[name]}
        return {int(d) for d in settings.WEEKEND_DAYS or []}

    def classify(self, enrollee: Any, day: date, *, holiday: Any, leave: LeaveInfo | None,
                 weekend: set[int]) -> DayInfo:
        if holiday is not None and not holiday.cancelled:
            return DayInfo(day, "public_holiday", holiday.name, holiday.is_paid,
                           {"source": holiday.source, **holiday.metadata})
        if day.weekday() in weekend:
            return DayInfo(day, "weekend", "Weekend", True, {})
        if leave is not None:
            return DayInfo(day, "leave", leave.label, leave.is_paid,
                           {"leave_type": leave.leave_type, **leave.metadata})
        return DayInfo(day, "workday", "", True, {})

    def get_day_info(self, enrollee: Any, day: date) -> DayInfo:
        return self._range_for(enrollee, day, day)[day]

    def _range_for(self, enrollee: Any, start: date, end: date) -> dict[date, DayInfo]:
        holidays = self.holidays.get_holidays(start, end, enrollee=enrollee)
        leaves = self.leave.get_leaves(enrollee, start, end) if enrollee is not None else {}
        weekend = self.weekend_days(enrollee)
        return {d: self.classify(enrollee, d, holiday=holidays.get(d), leave=leaves.get(d),
                                 weekend=weekend)
                for d in daterange(start, end)}

    def get_range(self, enrollee_or_queryset: Any, start: date,
                  end: date) -> dict[Any, dict[date, DayInfo]]:
        enrollees = ([enrollee_or_queryset] if hasattr(enrollee_or_queryset, "device_pin")
                     else list(enrollee_or_queryset))
        return {e.pk: self._range_for(e, start, end) for e in enrollees}
