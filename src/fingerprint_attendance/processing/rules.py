"""Default status rules, evaluated in the order listed in ``STATUS_RULES``.

A rule is ``rule(ctx: StatusContext) -> str | list[str] | None``. Returned statuses are
appended to ``ctx.statuses``; the first status becomes ``AttendanceDay.status``. Set
``ctx.stop = True`` to skip the remaining rules. Register custom statuses in
``fingerprint_attendance.registry.attendance_statuses``.
"""

from __future__ import annotations

from .base import StatusContext


def on_leave(ctx: StatusContext) -> str | None:
    if ctx.day_info.day_type == "leave" and not ctx.has_punches:
        ctx.stop = True
        return "on_leave"
    return None


def non_working_day(ctx: StatusContext) -> str | None:
    if ctx.day_info.is_working or ctx.day_info.day_type == "leave":
        return None
    ctx.stop = True
    if not ctx.has_punches:
        return "off"
    return "worked_on_holiday" if ctx.day_info.day_type == "public_holiday" \
        else "worked_on_off_day"


def absent(ctx: StatusContext) -> str | None:
    if not ctx.has_punches:
        ctx.stop = True
        return "absent"
    return None


def incomplete(ctx: StatusContext) -> str | None:
    return "incomplete" if len(ctx.punches) == 1 else None


def late(ctx: StatusContext) -> str | None:
    if ctx.shift is None or ctx.first_in is None:
        return None
    expected = ctx.shift.start_datetime(ctx.work_date, ctx.zone) + ctx.shift.grace_in
    if ctx.first_in > expected:
        ctx.extra["late_by_seconds"] = (ctx.first_in - expected + ctx.shift.grace_in) \
            .total_seconds()
        return "late"
    return None


def early_leave(ctx: StatusContext) -> str | None:
    if ctx.shift is None or ctx.last_out is None:
        return None
    expected = ctx.shift.end_datetime(ctx.work_date, ctx.zone) - ctx.shift.grace_out
    if ctx.last_out < expected:
        ctx.extra["early_by_seconds"] = (expected - ctx.last_out + ctx.shift.grace_out) \
            .total_seconds()
        return "early_leave"
    return None


def present(ctx: StatusContext) -> str | None:
    return "present" if ctx.has_punches else None
