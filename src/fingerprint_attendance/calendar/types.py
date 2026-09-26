"""Value objects returned by calendar, holiday, leave and schedule providers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..conf import parse_time_value
from ..registry import is_working_day_type


@dataclass(frozen=True)
class HolidayInfo:
    date: date
    name: str
    is_paid: bool = True
    #: ``True`` for an override that cancels a holiday coming from a lower-priority provider
    cancelled: bool = False
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LeaveInfo:
    date: date
    leave_type: str = "leave"
    label: str = "Leave"
    is_paid: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DayInfo:
    date: date
    day_type: str
    label: str = ""
    is_paid: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_working(self) -> bool:
        return is_working_day_type(self.day_type)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["date"] = self.date.isoformat()
        data["is_working"] = self.is_working
        return data


@dataclass(frozen=True)
class ShiftInfo:
    """An expected shift for one enrollee on one work date."""

    expected_start: time
    expected_end: time
    grace_in: timedelta = timedelta(0)
    grace_out: timedelta = timedelta(0)
    break_duration: timedelta = timedelta(0)
    crosses_midnight: bool = False
    name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ShiftInfo:
        start = parse_time_value(config["start"])
        end = parse_time_value(config["end"])
        crosses = bool(config.get("crosses_midnight", end <= start))
        return cls(
            expected_start=start,
            expected_end=end,
            grace_in=timedelta(minutes=int(config.get("grace_in_minutes", 0))),
            grace_out=timedelta(minutes=int(config.get("grace_out_minutes", 0))),
            break_duration=timedelta(minutes=int(config.get("break_minutes", 0))),
            crosses_midnight=crosses,
            name=str(config.get("name", "")),
            metadata=dict(config.get("metadata", {})),
        )

    def start_datetime(self, work_date: date, zone: ZoneInfo) -> datetime:
        return datetime.combine(work_date, self.expected_start, tzinfo=zone)

    def end_datetime(self, work_date: date, zone: ZoneInfo) -> datetime:
        end_date = work_date + timedelta(days=1) if self.crosses_midnight else work_date
        return datetime.combine(end_date, self.expected_end, tzinfo=zone)

    @property
    def planned_duration(self) -> timedelta:
        today = date(2000, 1, 3)
        utc = ZoneInfo("UTC")
        return self.end_datetime(today, utc) - self.start_datetime(today, utc) - self.break_duration

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expected_start": self.expected_start.strftime("%H:%M:%S"),
            "expected_end": self.expected_end.strftime("%H:%M:%S"),
            "grace_in_minutes": self.grace_in.total_seconds() / 60,
            "grace_out_minutes": self.grace_out.total_seconds() / 60,
            "break_minutes": self.break_duration.total_seconds() / 60,
            "crosses_midnight": self.crosses_midnight,
            "metadata": self.metadata,
        }
