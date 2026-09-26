"""Extensible registries.

Values such as day types, attendance statuses, punch sources, punch flags and punch states
are stored as plain strings, so consumers can register their own without migrations::

    from fingerprint_attendance.registry import day_types, attendance_statuses
    day_types.register("half_day", label="Half day", is_working=True)
    attendance_statuses.register("worked_remote", label="Worked remotely")
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Entry:
    key: str
    label: str
    meta: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["meta"][name]
        except KeyError:
            raise AttributeError(name) from None


class Registry:
    def __init__(self, name: str) -> None:
        self.name = name
        self._entries: dict[str, Entry] = {}

    def register(self, key: str, label: str | None = None, *, replace: bool = True,
                 **meta: Any) -> Entry:
        if not key or not isinstance(key, str):
            raise ValueError(f"{self.name}: key must be a non-empty string")
        if key in self._entries and not replace:
            raise ValueError(f"{self.name}: {key!r} is already registered")
        entry = Entry(key=key, label=label or key.replace("_", " ").capitalize(), meta=meta)
        self._entries[key] = entry
        return entry

    def unregister(self, key: str) -> None:
        self._entries.pop(key, None)

    def get(self, key: str) -> Entry | None:
        return self._entries.get(key)

    def __getitem__(self, key: str) -> Entry:
        return self._entries[key]

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __iter__(self) -> Iterator[Entry]:
        return iter(self._entries.values())

    def keys(self) -> list[str]:
        return list(self._entries)

    def choices(self) -> list[tuple[str, str]]:
        return [(e.key, e.label) for e in self._entries.values()]


# --------------------------------------------------------------------------- day types
day_types = Registry("day_types")
day_types.register("workday", "Working day", is_working=True)
day_types.register("weekend", "Weekend", is_working=False)
day_types.register("public_holiday", "Public holiday", is_working=False)
day_types.register("leave", "Leave", is_working=False)
day_types.register("off", "Day off", is_working=False)


def is_working_day_type(key: str) -> bool:
    entry = day_types.get(key)
    return bool(entry and entry.meta.get("is_working"))


# --------------------------------------------------------------------------- statuses
attendance_statuses = Registry("attendance_statuses")
for _key, _label in (
    ("present", "Present"),
    ("absent", "Absent"),
    ("late", "Late"),
    ("early_leave", "Early leave"),
    ("incomplete", "Incomplete (missing punch)"),
    ("worked_on_off_day", "Worked on a day off"),
    ("worked_on_holiday", "Worked on a holiday"),
    ("on_leave", "On leave"),
    ("off", "Non-working day"),
):
    attendance_statuses.register(_key, _label)

# --------------------------------------------------------------------------- punches
punch_sources = Registry("punch_sources")
for _key in ("adms", "pull", "import", "manual"):
    punch_sources.register(_key)

punch_flags = Registry("punch_flags")
punch_flags.register("clock_drift", "Device clock drift")
punch_flags.register("future", "In the future")
punch_flags.register("unknown_pin", "Unknown PIN")
punch_flags.register("late_sync", "Synced late")
punch_flags.register("duplicate", "Soft duplicate", exclude_from_processing=True)
punch_flags.register("manual", "Manual entry")
punch_flags.register("pending_device", "From an unapproved device")

punch_states = Registry("punch_states")
for _key, _label in (
    ("check_in", "Check in"),
    ("check_out", "Check out"),
    ("break_out", "Break out"),
    ("break_in", "Break in"),
    ("overtime_in", "Overtime in"),
    ("overtime_out", "Overtime out"),
    ("unknown", "Unknown"),
):
    punch_states.register(_key, _label)

IN_STATES = frozenset({"check_in", "break_in", "overtime_in"})
OUT_STATES = frozenset({"check_out", "break_out", "overtime_out"})
