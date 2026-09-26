"""A custom attendance processor: first-in/last-out plus overtime for eligible staff."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fingerprint_attendance.processing.default import FirstInLastOutProcessor


class OvertimeProcessor(FirstInLastOutProcessor):
    version = "overtime-1"
    overtime_after = timedelta(minutes=30)

    def apply_rules(self, ctx: Any) -> None:
        super().apply_rules(ctx)
        employee = ctx.enrollee.employee
        if not getattr(employee, "overtime_eligible", False) or ctx.shift is None:
            return
        if ctx.worked is None:
            return
        extra = ctx.worked - ctx.shift.planned_duration
        if extra >= self.overtime_after:
            ctx.statuses.append("overtime")
            ctx.extra["overtime_seconds"] = extra.total_seconds()
