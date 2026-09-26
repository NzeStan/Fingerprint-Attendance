"""Default processor: first-in / last-out per work date.

Business rules (lateness, overtime, shifts, holidays) are not hard-coded: day types come from
the calendar provider, expected shifts from the schedule provider and statuses from
``STATUS_RULES``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db import transaction

from .. import payloads
from ..calendar.providers import get_calendar_provider, get_schedule_provider
from ..conf import settings
from ..events import emit
from ..utils.timeutils import now
from .base import BaseAttendanceProcessor, EffectivePunch, StatusContext


class FirstInLastOutProcessor(BaseAttendanceProcessor):
    version = "filo-1"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calendar = get_calendar_provider()
        self.schedules = get_schedule_provider()

    # -- extension points ------------------------------------------------------------------
    def effective_punches(self, enrollee: Any, work_date: date) -> list[EffectivePunch]:
        """Punches in the day window, soft duplicates removed, adjustments applied."""
        from ..models import Punch, PunchAdjustment

        start, end = self.boundary.window(work_date, enrollee)
        # include punches whose adjusted time may move them into this day
        span = end - start
        raw = list(Punch.objects.effective().filter(enrollee=enrollee)
                   .filter(punched_at__gte=start - span, punched_at__lt=end + span)
                   .order_by("punched_at", "id"))
        adjustments: dict[int, list[Any]] = {}
        for adj in PunchAdjustment.objects.filter(punch__in=raw).order_by("created_at", "id"):
            adjustments.setdefault(adj.punch_id, []).append(adj)
        result = []
        for punch in raw:
            when, state, void = punch.punched_at, punch.state, False
            for adj in adjustments.get(punch.pk, []):
                if adj.action == "void":
                    void = True
                elif adj.action == "set_state" and adj.new_state:
                    state = adj.new_state
                elif adj.action == "set_time" and adj.new_punched_at:
                    when = adj.new_punched_at
            if void or not (start <= when < end):
                continue
            result.append(EffectivePunch(punch.pk, when, state, punch.source, punch.flag_list))
        result.sort(key=lambda p: (p.punched_at, p.punch_id))
        return result

    def build_context(self, enrollee: Any, work_date: date,
                      punches: list[EffectivePunch]) -> StatusContext:
        day_info = self.calendar.get_day_info(enrollee, work_date)
        shift = self.schedules.get_expected_shift(enrollee, work_date)
        first_in = punches[0].punched_at if punches else None
        last_out = punches[-1].punched_at if len(punches) >= 2 else None
        worked = (last_out - first_in) if (first_in and last_out) else None
        if worked is not None and shift is not None and shift.break_duration:
            worked = max(worked - shift.break_duration, timedelta(0))
        return StatusContext(enrollee=enrollee, work_date=work_date, day_info=day_info,
                             shift=shift, punches=punches, first_in=first_in,
                             last_out=last_out, worked=worked, zone=self.boundary.zone)

    def apply_rules(self, ctx: StatusContext) -> None:
        for rule in settings.import_("STATUS_RULES") or []:
            outcome = rule(ctx)
            if outcome:
                for status in [outcome] if isinstance(outcome, str) else list(outcome):
                    if status not in ctx.statuses:
                        ctx.statuses.append(status)
            if ctx.stop:
                break

    # -- implementation --------------------------------------------------------------------
    def process_day(self, enrollee: Any, work_date: date, *, create_empty: bool = False) -> Any:
        from ..models import AttendanceDay

        punches = self.effective_punches(enrollee, work_date)
        existing = AttendanceDay.objects.filter(enrollee=enrollee, work_date=work_date).first()
        if not punches and existing is None and not create_empty:
            return None
        ctx = self.build_context(enrollee, work_date, punches)
        self.apply_rules(ctx)
        values = {
            "first_in": ctx.first_in,
            "last_out": ctx.last_out,
            "worked_duration": ctx.worked,
            "punch_count": len(punches),
            "status": ctx.statuses[0] if ctx.statuses else "",
            "statuses": ctx.statuses,
            "day_type": ctx.day_info.day_type,
            "day_label": ctx.day_info.label[:200],
            "is_paid": ctx.day_info.is_paid,
            "expected_shift": ctx.shift.to_dict() if ctx.shift else None,
            "computed_at": now(),
            "processor_version": self.version,
            "extra": {**ctx.extra, "punch_ids": [p.punch_id for p in punches]},
        }
        with transaction.atomic():
            day, _ = AttendanceDay.objects.update_or_create(
                enrollee=enrollee, work_date=work_date, defaults=values)
        emit("attendance_computed", payloads.attendance_day_payload(day), attendance_day=day)
        return day
