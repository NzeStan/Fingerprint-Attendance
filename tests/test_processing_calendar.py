"""Attendance processing, day boundaries, calendar providers and recompute triggers."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from fingerprint_attendance import services
from fingerprint_attendance.calendar.holidays import (
    ChainedHolidayProvider,
    DatabaseHolidayProvider,
    PythonHolidaysProvider,
    SettingsHolidayProvider,
)
from fingerprint_attendance.calendar.providers import (
    DatabaseScheduleProvider,
    DefaultCalendarProvider,
    SettingsScheduleProvider,
)
from fingerprint_attendance.calendar.types import LeaveInfo, ShiftInfo
from fingerprint_attendance.ingestion.candidates import PunchCandidate
from fingerprint_attendance.ingestion.pipeline import ingest_punches
from fingerprint_attendance.models import (
    AttendanceDay,
    DeviceGroup,
    Holiday,
    ShiftAssignment,
    WorkSchedule,
)
from fingerprint_attendance.processing.boundaries import (
    CalendarDayBoundary,
    OffsetDayBoundary,
    ShiftAwareDayBoundary,
)
from tests.conftest import local
from tests.testapp import calendar as test_calendar

pytestmark = pytest.mark.django_db
LAGOS = ZoneInfo("Africa/Lagos")

# 2026-01-05 is a Monday; 2026-01-10 a Saturday.


def punch(enrollee, device, *moments):
    ingest_punches([PunchCandidate(pin=enrollee.device_pin, local_time=m, device=device)
                    for m in moments])


def backdate(*enrollees):
    """Absences are only generated for dates after the enrollee was created."""
    from fingerprint_attendance.models import Enrollee

    Enrollee.objects.filter(pk__in=[e.pk for e in enrollees]).update(
        created_at=datetime(2025, 1, 1, tzinfo=LAGOS))


def day(enrollee, d):
    return AttendanceDay.objects.get(enrollee=enrollee, work_date=d)


@pytest.fixture(autouse=True)
def _reset_calendar():
    test_calendar.LEAVES.clear()
    test_calendar.EVENTS.clear()


def test_first_in_last_out(make_enrollee, make_device, fpa):
    fpa(DEFAULT_SCHEDULE={"start": "09:00", "end": "17:00", "grace_in_minutes": 10})
    e, d = make_enrollee(), make_device()
    punch(e, d, local(2026, 1, 5, 9, 5), local(2026, 1, 5, 12), local(2026, 1, 5, 17, 30))
    result = day(e, date(2026, 1, 5))
    assert result.status == "present" and result.statuses == ["present"]
    assert result.first_in == datetime(2026, 1, 5, 9, 5, tzinfo=LAGOS)
    assert result.last_out == datetime(2026, 1, 5, 17, 30, tzinfo=LAGOS)
    assert result.worked_duration == timedelta(hours=8, minutes=25)
    assert result.punch_count == 3 and result.day_type == "workday"
    assert result.expected_shift["expected_start"] == "09:00:00"


def test_late_early_and_incomplete(make_enrollee, make_device, fpa):
    fpa(DEFAULT_SCHEDULE={"start": "09:00", "end": "17:00", "grace_in_minutes": 5,
                          "break_minutes": 60})
    e, d = make_enrollee(), make_device()
    punch(e, d, local(2026, 1, 5, 9, 20), local(2026, 1, 5, 16))
    result = day(e, date(2026, 1, 5))
    assert result.statuses == ["late", "early_leave", "present"] and result.status == "late"
    assert result.extra["late_by_seconds"] == 20 * 60
    assert result.worked_duration == timedelta(hours=5, minutes=40)
    e2 = make_enrollee()
    punch(e2, d, local(2026, 1, 6, 8, 55))
    assert day(e2, date(2026, 1, 6)).status == "incomplete"


def test_weekend_and_holiday_classification(make_enrollee, make_device, fpa):
    fpa(HOLIDAYS=[{"date": "2026-01-01", "name": "New Year", "recurring": True}])
    e, d = make_enrollee(), make_device()
    punch(e, d, local(2026, 1, 10, 9), local(2026, 1, 10, 12))
    assert day(e, date(2026, 1, 10)).status == "worked_on_off_day"
    punch(e, d, local(2027, 1, 1, 9), local(2027, 1, 1, 12))
    holiday = day(e, date(2027, 1, 1))
    assert holiday.status == "worked_on_holiday" and holiday.day_label == "New Year"


def test_weekend_by_group(make_enrollee, fpa):
    fpa(WEEKEND_DAYS_BY_GROUP={"Gulf": [4, 5]})
    gulf = DeviceGroup.objects.create(name="Gulf")
    e = make_enrollee(groups=[gulf])
    calendar = DefaultCalendarProvider()
    assert calendar.get_day_type(e, date(2026, 1, 9)) == "weekend"  # Friday
    assert calendar.get_day_type(e, date(2026, 1, 11)) == "workday"  # Sunday
    other = make_enrollee()
    assert calendar.get_day_type(other, date(2026, 1, 11)) == "weekend"


def test_database_holiday_beats_other_providers(make_enrollee, fpa):
    fpa(HOLIDAYS=[{"date": "2026-10-01", "name": "Independence Day"}])
    e = make_enrollee()
    calendar = DefaultCalendarProvider()
    assert calendar.get_day_info(e, date(2026, 10, 1)).day_type == "public_holiday"
    # the government moves the holiday: cancel it and add the new date at runtime
    Holiday.objects.create(date=date(2026, 10, 1), name="moved", is_cancelled=True)
    Holiday.objects.create(date=date(2026, 10, 2), name="Independence Day (observed)")
    calendar = DefaultCalendarProvider()
    assert calendar.get_day_info(e, date(2026, 10, 1)).day_type == "workday"
    info = calendar.get_day_info(e, date(2026, 10, 2))
    assert info.day_type == "public_holiday" and "observed" in info.label


def test_holiday_scopes(make_enrollee):
    hq = DeviceGroup.objects.create(name="HQ")
    e_hq, e_other = make_enrollee(groups=[hq]), make_enrollee()
    Holiday.objects.create(date=date(2026, 3, 3), name="HQ day", scope="device_group",
                           device_group=hq)
    Holiday.objects.create(date=date(2026, 3, 4), name="Birthday", scope="enrollee",
                           enrollee=e_other)
    Holiday.objects.create(date=date(2026, 3, 4), name="Everyone", scope="all")
    provider = DatabaseHolidayProvider()
    assert provider.get_holiday(date(2026, 3, 3), enrollee=e_hq).name == "HQ day"
    assert provider.get_holiday(date(2026, 3, 3), enrollee=e_other) is None
    assert provider.get_holiday(date(2026, 3, 4), enrollee=e_other).name == "Birthday"
    assert provider.get_holiday(date(2026, 3, 4), enrollee=e_hq).name == "Everyone"
    Holiday.objects.create(date=date(2024, 2, 29), name="Leap", recurring=True)
    assert date(2028, 2, 29) in provider.get_holidays(date(2026, 1, 1), date(2028, 12, 31))


def test_python_holidays_and_chain(fpa):
    fpa(HOLIDAYS_COUNTRY="NG")
    ng = PythonHolidaysProvider()
    holidays = ng.get_holidays(date(2026, 1, 1), date(2026, 12, 31))
    assert date(2026, 10, 1) in holidays  # Nigerian Independence Day
    Holiday.objects.create(date=date(2026, 10, 1), name="cancelled", is_cancelled=True)
    chain = ChainedHolidayProvider([DatabaseHolidayProvider(), ng])
    assert chain.get_holiday(date(2026, 10, 1)) is None
    assert chain.get_holiday(date(2026, 12, 25)) is not None
    fpa(HOLIDAYS_COUNTRY=None)
    assert PythonHolidaysProvider().get_holidays(date(2026, 1, 1), date(2026, 1, 2)) == {}
    fpa(HOLIDAY_PROVIDER_CHAIN=["database", "holidays"], HOLIDAYS_COUNTRY="NG")
    assert ChainedHolidayProvider().get_holiday(date(2026, 12, 25)) is not None
    assert SettingsHolidayProvider().get_holidays(date(2026, 1, 1), date(2026, 1, 1)) == {}


def test_leave_overrides_absence(make_enrollee, fpa):
    fpa(LEAVE_PROVIDER="tests.testapp.calendar.DictLeaveProvider")
    e = make_enrollee()
    test_calendar.LEAVES[(e.pk, date(2026, 1, 6))] = LeaveInfo(date(2026, 1, 6), "annual",
                                                                "Annual leave")
    backdate(e)
    services.generate_absences(date(2026, 1, 6), force=True)
    services.generate_absences(date(2026, 1, 7), force=True)
    assert day(e, date(2026, 1, 6)).status == "on_leave"
    assert day(e, date(2026, 1, 6)).day_label == "Annual leave"
    assert day(e, date(2026, 1, 7)).status == "absent"


def test_absence_generation_rules(make_enrollee, make_device, fpa):
    e, d = make_enrollee(), make_device()
    newcomer = make_enrollee()
    backdate(e)
    punch(e, d, local(2026, 1, 5, 9), local(2026, 1, 5, 17))
    assert services.generate_absences(date(2026, 1, 10), force=True) == 1  # not the newcomer
    assert not AttendanceDay.objects.filter(enrollee=newcomer).exists()
    assert day(e, date(2026, 1, 10)).status == "off"  # weekend
    services.generate_absences(date(2026, 1, 5), force=True)
    assert day(e, date(2026, 1, 5)).status == "present"  # not overwritten
    future = date.today() + timedelta(days=5)
    assert services.generate_absences(future) == 0  # before cut-off
    fpa(ABSENCE_GENERATION_ENABLED=False)
    assert services.generate_absences(date(2026, 1, 6), force=True) == 0


def test_custom_day_type_and_status_rule(make_enrollee, make_device, fpa):
    from fingerprint_attendance.conf import settings

    fpa(CALENDAR_PROVIDER="tests.testapp.calendar.EventCalendarProvider",
        STATUS_RULES=["tests.testapp.calendar.event_rule", *settings.STATUS_RULES])
    test_calendar.EVENTS.add(date(2026, 1, 7))
    e, d = make_enrollee(), make_device()
    punch(e, d, local(2026, 1, 7, 10), local(2026, 1, 7, 15))
    result = day(e, date(2026, 1, 7))
    assert result.day_type == "company_event" and result.status == "attended_event"


def test_holiday_added_after_punches_triggers_recompute(make_enrollee, make_device):
    e, d = make_enrollee(), make_device()
    punch(e, d, local(2026, 1, 8, 9), local(2026, 1, 8, 17))
    assert day(e, date(2026, 1, 8)).status == "present"
    holiday = Holiday.objects.create(date=date(2026, 1, 8), name="Surprise holiday")
    assert day(e, date(2026, 1, 8)).status == "worked_on_holiday"
    holiday.date = date(2026, 1, 9)
    holiday.save()
    assert day(e, date(2026, 1, 8)).status == "present"
    holiday.delete()


def test_calendar_changed_service(make_enrollee, make_device, fpa):
    from fingerprint_attendance.signals import calendar_changed

    e, d = make_enrollee(), make_device()
    punch(e, d, local(2026, 1, 8, 9), local(2026, 1, 8, 17))
    fpa(LEAVE_PROVIDER="tests.testapp.calendar.DictLeaveProvider")
    test_calendar.LEAVES[(e.pk, date(2026, 1, 8))] = LeaveInfo(date(2026, 1, 8))
    seen = []
    calendar_changed.connect(lambda **kw: seen.append(kw["enrollee_ids"]), weak=False,
                             dispatch_uid="t-cal")
    try:
        services.calendar_changed(date(2026, 1, 8), date(2026, 1, 8), enrollee_ids=[e.pk])
    finally:
        calendar_changed.disconnect(dispatch_uid="t-cal")
    assert seen == [[e.pk]]
    assert day(e, date(2026, 1, 8)).day_type == "leave"


def test_night_shift_crossing_midnight(make_enrollee, make_device, fpa):
    fpa(DEFAULT_SCHEDULE={"start": "22:00", "end": "06:00", "grace_in_minutes": 5},
        DAY_BOUNDARY_RESOLVER="shift_aware")
    e, d = make_enrollee(), make_device()
    punch(e, d, local(2026, 1, 5, 21, 55), local(2026, 1, 6, 6, 10))
    night = day(e, date(2026, 1, 5))
    assert night.status == "present" and night.worked_duration == timedelta(hours=8,
                                                                               minutes=15)
    assert not AttendanceDay.objects.filter(work_date=date(2026, 1, 6)).exists()


def test_boundaries():
    cal = CalendarDayBoundary(LAGOS)
    t = datetime(2026, 1, 5, 23, 30, tzinfo=ZoneInfo("UTC"))  # 00:30 Lagos
    assert cal.work_date(t) == date(2026, 1, 6)
    offset = OffsetDayBoundary(LAGOS, time(4, 0))
    assert offset.work_date(t) == date(2026, 1, 5)
    start, end = offset.window(date(2026, 1, 5))
    assert start == datetime(2026, 1, 5, 4, tzinfo=LAGOS) and end - start == timedelta(days=1)


def test_shift_aware_boundary_consistency(make_enrollee, fpa):
    fpa(DEFAULT_SCHEDULE={"start": "22:00", "end": "06:00"})
    e = make_enrollee()
    b = ShiftAwareDayBoundary(LAGOS, margin=timedelta(hours=2))
    start, end = b.window(date(2026, 1, 6), e)
    assert start == datetime(2026, 1, 6, 8, tzinfo=LAGOS)
    assert end == datetime(2026, 1, 7, 8, tzinfo=LAGOS)
    for hour in range(0, 48, 3):
        instant = datetime(2026, 1, 6, tzinfo=LAGOS) + timedelta(hours=hour)
        wd = b.work_date(instant, e)
        s, en = b.window(wd, e)
        assert s <= instant < en
    assert b.work_date(datetime(2026, 1, 6, 7, tzinfo=LAGOS), None) == date(2026, 1, 6)


def test_schedule_providers(make_enrollee, fpa):
    fpa(DEFAULT_SCHEDULE={"start": "08:00", "end": "16:00", "weekdays": [0, 1, 2, 3, 4]})
    e = make_enrollee()
    provider = SettingsScheduleProvider()
    assert provider.get_expected_shift(e, date(2026, 1, 5)).expected_start == time(8)
    assert provider.get_expected_shift(e, date(2026, 1, 10)) is None
    assert len(provider.get_range(e, date(2026, 1, 5), date(2026, 1, 11))) == 7
    group = DeviceGroup.objects.create(name="Ops")
    e.groups.add(group)
    day_shift = WorkSchedule.objects.create(name="Day", start_time=time(8),
                                            end_time=time(16))
    night = WorkSchedule.objects.create(name="Night", start_time=time(22), end_time=time(6),
                                        weekdays=[0, 1, 2, 3, 4, 5, 6])
    assert night.crosses_midnight
    ShiftAssignment.objects.create(schedule=day_shift, device_group=group,
                                   start_date=date(2026, 1, 1))
    ShiftAssignment.objects.create(schedule=night, enrollee=e, start_date=date(2026, 1, 7),
                                   end_date=date(2026, 1, 8))
    db = DatabaseScheduleProvider()
    assert db.get_expected_shift(e, date(2026, 1, 5)).name == "Day"
    assert db.get_expected_shift(e, date(2026, 1, 7)).name == "Night"
    assert db.get_expected_shift(e, date(2026, 1, 10)) is None  # Day schedule is Mon-Fri
    assert db.get_expected_shift(make_enrollee(), date(2026, 1, 5)) is None


def test_schedule_change_triggers_recompute(make_enrollee, make_device, fpa):
    fpa(SCHEDULE_PROVIDER="database", SCHEDULE_MODELS_ENABLED=True)
    e, d = make_enrollee(), make_device()
    ingest_punches([PunchCandidate(pin=e.device_pin, local_time=local(2026, 1, 5, 9, 30),
                                   device=d),
                    PunchCandidate(pin=e.device_pin, local_time=local(2026, 1, 5, 17),
                                   device=d)])
    assert day(e, date(2026, 1, 5)).status == "present"
    schedule = WorkSchedule.objects.create(name="Early", start_time=time(8),
                                           end_time=time(17))
    ShiftAssignment.objects.create(schedule=schedule, enrollee=e, start_date=date(2026, 1, 1))
    assert day(e, date(2026, 1, 5)).status == "late"
    schedule.start_time = time(10)
    schedule.save()
    assert day(e, date(2026, 1, 5)).status == "present"


def test_adjustments_affect_processing(make_enrollee, make_device):
    e, d = make_enrollee(), make_device()
    punch(e, d, local(2026, 1, 5, 9), local(2026, 1, 5, 17))
    last = e.punches.order_by("punched_at").last()
    services.adjust_punch(last, action="void", reason="test")
    assert day(e, date(2026, 1, 5)).status == "incomplete"
    first = e.punches.order_by("punched_at").first()
    services.adjust_punch(first, action="set_time",
                          new_punched_at=datetime(2026, 1, 6, 9, tzinfo=LAGOS))
    assert day(e, date(2026, 1, 5)).punch_count == 0
    assert day(e, date(2026, 1, 6)).punch_count == 1


def test_recompute_and_disabled_processor(make_enrollee, make_device, fpa):
    e, d = make_enrollee(), make_device()
    fpa(PROCESS_ON_INGEST=False)
    punch(e, d, local(2026, 1, 5, 9), local(2026, 1, 5, 17))
    assert not AttendanceDay.objects.exists()
    assert services.recompute(start=date(2026, 1, 1), end=date(2026, 1, 10)) == 1
    assert services.recompute(start=date(2026, 1, 5), end=date(2026, 1, 5),
                              enrollee_ids=[e.pk]) == 1
    fpa(ATTENDANCE_PROCESSOR=None)
    assert services.recompute(start=date(2026, 1, 1), end=date(2026, 1, 10)) == 0
    assert services.process_pairs([(e.pk, "2026-01-05")]) == 0
    assert services.generate_absences(date(2026, 1, 6), force=True) == 0


def test_soft_duplicates_ignored_by_processor(make_enrollee, make_device):
    e, d = make_enrollee(), make_device()
    punch(e, d, local(2026, 1, 5, 9), local(2026, 1, 5, 9, 0, 20))
    assert day(e, date(2026, 1, 5)).status == "incomplete"


def test_shift_info_helpers():
    shift = ShiftInfo.from_config({"start": "22:00", "end": "06:00", "break_minutes": 30})
    assert shift.crosses_midnight and shift.planned_duration == timedelta(hours=7, minutes=30)
    assert shift.to_dict()["break_minutes"] == 30
