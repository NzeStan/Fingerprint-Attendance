"""Internal receivers wiring model lifecycle events to services (connected in ``ready()``)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from .models import Device, Enrollee, Holiday, ShiftAssignment, WorkSchedule
from .utils.logging import get_logger

logger = get_logger(__name__)


@receiver(pre_delete, sender=Enrollee, dispatch_uid="fpa_enrollee_pre_delete")
def _enrollee_pre_delete(sender: Any, instance: Any, **kwargs: Any) -> None:
    from .services.enrollees import handle_enrollee_pre_delete

    handle_enrollee_pre_delete(instance)


@receiver(m2m_changed, sender=Enrollee.groups.through, dispatch_uid="fpa_enrollee_groups")
def _enrollee_groups_changed(sender: Any, instance: Any, action: str, reverse: bool,
                             pk_set: Any, **kwargs: Any) -> None:
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    from .tasks.backends import enqueue

    if reverse:  # group.enrollees.add(...)
        ids = list(pk_set or [])
        for enrollee_id in ids:
            enqueue("fingerprint_attendance.tasks.jobs.sync_enrollee", enrollee_id)
    elif isinstance(instance, Enrollee) and instance.is_active:
        enqueue("fingerprint_attendance.tasks.jobs.sync_enrollee", instance.pk)


@receiver(m2m_changed, sender=Device.groups.through, dispatch_uid="fpa_device_groups")
def _device_groups_changed(sender: Any, instance: Any, action: str, reverse: bool,
                           pk_set: Any, **kwargs: Any) -> None:
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    from .tasks.backends import enqueue

    device_ids = list(pk_set or []) if reverse else [instance.pk]
    for device_id in device_ids:
        enqueue("fingerprint_attendance.tasks.jobs.reconcile_device_scope", device_id)


# --------------------------------------------------------------------------- calendar changes


def _holiday_dates(instance: Any, value: date | None = None) -> list[date]:
    base = value or instance.date
    if not instance.recurring:
        return [base]
    from django.db.models import Max, Min

    from .models import AttendanceDay

    span = AttendanceDay.objects.aggregate(lo=Min("work_date"), hi=Max("work_date"))
    if not span["lo"]:
        return []
    out = []
    for year in range(span["lo"].year, span["hi"].year + 1):
        try:
            out.append(date(year, base.month, base.day))
        except ValueError:
            continue
    return out


def _holiday_enrollees(instance: Any) -> list[int] | None:
    from .constants import HolidayScope

    if instance.scope == HolidayScope.ENROLLEE and instance.enrollee_id:
        return [instance.enrollee_id]
    if instance.scope == HolidayScope.DEVICE_GROUP and instance.device_group_id:
        return list(Enrollee.objects.filter(groups=instance.device_group_id)
                    .values_list("pk", flat=True))
    return None


@receiver(pre_save, sender=Holiday, dispatch_uid="fpa_holiday_pre_save")
def _holiday_pre_save(sender: Any, instance: Any, **kwargs: Any) -> None:
    if instance.pk:
        previous = Holiday.objects.filter(pk=instance.pk).values_list("date", flat=True).first()
        instance._fpa_old_date = previous


@receiver(post_save, sender=Holiday, dispatch_uid="fpa_holiday_saved")
@receiver(post_delete, sender=Holiday, dispatch_uid="fpa_holiday_deleted")
def _holiday_changed(sender: Any, instance: Any, **kwargs: Any) -> None:
    from .services.attendance import calendar_changed

    dates = set(_holiday_dates(instance))
    old = getattr(instance, "_fpa_old_date", None)
    if old and old != instance.date:
        dates |= set(_holiday_dates(instance, old))
    enrollees = _holiday_enrollees(instance)
    for day in sorted(dates):
        calendar_changed(day, day, enrollee_ids=enrollees, reason=f"holiday {instance.name}")


def _assignment_scope(assignment: Any) -> tuple[date, date, list[int]]:
    from django.utils import timezone

    today = timezone.localdate()
    end = min(assignment.end_date or today, today)
    ids: list[int] = []
    if assignment.enrollee_id:
        ids.append(assignment.enrollee_id)
    if assignment.device_group_id:
        ids.extend(Enrollee.objects.filter(groups=assignment.device_group_id)
                   .values_list("pk", flat=True))
    return assignment.start_date, end, ids


@receiver(post_save, sender=ShiftAssignment, dispatch_uid="fpa_assignment_saved")
@receiver(post_delete, sender=ShiftAssignment, dispatch_uid="fpa_assignment_deleted")
def _assignment_changed(sender: Any, instance: Any, **kwargs: Any) -> None:
    from .services.attendance import calendar_changed

    start, end, ids = _assignment_scope(instance)
    if ids and start <= end:
        calendar_changed(start, end, enrollee_ids=ids, reason="shift assignment changed")


@receiver(post_save, sender=WorkSchedule, dispatch_uid="fpa_schedule_saved")
def _schedule_changed(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    if created:
        return
    from .services.attendance import calendar_changed

    for assignment in instance.assignments.all():
        start, end, ids = _assignment_scope(assignment)
        if ids and start <= end:
            # bound very old assignments to the last 400 days
            start = max(start, end - timedelta(days=400))
            calendar_changed(start, end, enrollee_ids=ids,
                             reason=f"schedule {instance.name} changed")
