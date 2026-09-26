# Calendar, holidays, leave and schedules

Weekends, public holidays, leave and shifts are **yours to define**. The processor only talks
to provider interfaces, each set by a dotted-path setting.

| Setting | Interface | Default |
|---|---|---|
| `CALENDAR_PROVIDER` | `BaseCalendarProvider` (`get_day_info`, `get_day_type`, `get_range`) | `DefaultCalendarProvider`: public holiday > weekend > leave > working day |
| `HOLIDAY_PROVIDER` | `BaseHolidayProvider` (`get_holidays(start, end, enrollee=)`) | `chained` over `HOLIDAY_PROVIDER_CHAIN = ["database", "settings"]` |
| `LEAVE_PROVIDER` | `BaseLeaveProvider` (`get_leaves(enrollee, start, end)`) | `NullLeaveProvider` (connect your HR system) |
| `SCHEDULE_PROVIDER` | `BaseScheduleProvider` (`get_expected_shift(enrollee, date) -> ShiftInfo`) | `settings` (`DEFAULT_SCHEDULE`); `database` uses `WorkSchedule` / `ShiftAssignment` |

## Holidays

- `SettingsHolidayProvider`: `HOLIDAYS = [{"date": "2026-10-01", "name": "Independence Day", "recurring": true}]`
- `DatabaseHolidayProvider`: `Holiday` rows (admin and `/holidays/` API). Scope is `all`,
  `device_group` or `enrollee` (most specific wins). Rows can recur yearly, and an
  `is_cancelled` row cancels a holiday from lower-priority providers.
- `PythonHolidaysProvider` (`[holidays]` extra): `HOLIDAYS_COUNTRY="NG"`, optional
  `HOLIDAYS_SUBDIVISION`.
- `ChainedHolidayProvider`: the first provider with **any** entry for a date decides that date.

Some governments declare or move holidays at short notice (Nigeria often does). Put
`database` first in the chain. To move a holiday, add a cancelled row on the old date and a
new row on the new date. To add a surprise holiday, add one row. Days already computed are
recomputed automatically.

```python
FINGERPRINT_ATTENDANCE = {
    "HOLIDAY_PROVIDER_CHAIN": ["database", "holidays"],
    "HOLIDAYS_COUNTRY": "NG",
    "WEEKEND_DAYS": [5, 6],
    "WEEKEND_DAYS_BY_GROUP": {"Gulf office": [4, 5]},
}
```

## Day types and statuses

Day types (`workday`, `weekend`, `public_holiday`, `leave`, `off`) and statuses (`present`,
`absent`, `late`, `early_leave`, `incomplete`, `worked_on_off_day`, `worked_on_holiday`,
`on_leave`, `off`) are open registries:

```python
from fingerprint_attendance.registry import day_types, attendance_statuses
day_types.register("company_event", "Company event", is_working=True)
attendance_statuses.register("attended_event", "Attended event")
```

`STATUS_RULES` is an ordered list of callables `rule(ctx) -> str | list[str] | None`. Returned
statuses are appended, the first becomes `AttendanceDay.status`, and `ctx.stop = True` ends
evaluation. Add or replace one rule without touching the rest:

```python
from fingerprint_attendance.conf import SPEC_BY_NAME
FINGERPRINT_ATTENDANCE["STATUS_RULES"] = ["myapp.rules.company_event",
                                           *SPEC_BY_NAME["STATUS_RULES"].default]
```

## Schedules and overnight shifts

`DEFAULT_SCHEDULE = {"start": "09:00", "end": "17:00", "grace_in_minutes": 10,
"grace_out_minutes": 5, "break_minutes": 60, "weekdays": [0,1,2,3,4]}`. Without a schedule
there are no late or early statuses.

With `SCHEDULE_MODELS_ENABLED` and `SCHEDULE_PROVIDER="database"`, `ShiftAssignment` links a
`WorkSchedule` to an enrollee or device group over a date range. The highest priority wins,
and enrollee-specific beats group-level.

For night shifts use `DAY_BOUNDARY_RESOLVER="shift_aware"`. Punches up to
`OVERNIGHT_SHIFT_MARGIN` after an overnight shift ends belong to the date the shift started.
`offset` (with `DAY_START_TIME`) is a simpler alternative.

## Absences

`fpa_generate_absences --date` (or the periodic job) creates day rows for every active
enrollee (created before that date) after `ABSENCE_CUTOFF_TIME`: `absent` on working days
without punches, `on_leave`, `off` on non-working days. It is switched with
`ABSENCE_GENERATION_ENABLED`.

## Recompute on change

Holiday, schedule and assignment changes recompute affected days automatically. Call
`services.calendar_changed(start, end, enrollee_ids=[...])` when leave or rosters change in
your own systems. Punches on non-working days are always stored; only their classification
changes.

## API

`GET /api/fingerprint/v1/calendar/?enrollee=<id>&from=2026-01-01&to=2026-01-31` returns
resolved days and expected shifts, using the same logic as the processor, so front ends and
reports agree with `AttendanceDay`.
