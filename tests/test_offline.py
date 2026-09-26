"""Offline operation and catch-up sync (section 10a): no loss, no duplicates, no re-timing."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from fingerprint_attendance import services
from fingerprint_attendance.exceptions import UnsafeOperation
from fingerprint_attendance.models import AttendanceDay, Device, DeviceCommand, Punch
from fingerprint_attendance.signals import backlog_synced, device_clock_drift
from tests.conftest import local

pytestmark = pytest.mark.django_db
LAGOS = ZoneInfo("Africa/Lagos")


@pytest.fixture
def backlog_events():
    events = []
    backlog_synced.connect(lambda **kw: events.append(kw), weak=False, dispatch_uid="t-bl")
    yield events
    backlog_synced.disconnect(dispatch_uid="t-bl")


def test_offline_for_days_then_reconnect(simulator, make_enrollee, backlog_events):
    enrollee = make_enrollee(pin="100")
    sim = simulator()
    sim.go_offline()
    start = local(2026, 3, 2, 8, 0)
    for i in range(3000):  # ~10 days of busy punching
        sim.punch("100" if i % 3 == 0 else str(200 + i % 50),
                  start + timedelta(minutes=5 * i))
    assert Punch.objects.count() == 0 and len(sim.pending()) == 3000
    responses = sim.go_online()
    assert all(r.status == 200 for r in responses) and len(responses) == 6  # 500-line batches
    assert Punch.objects.count() == 3000 and sim.pending() == []
    # device time is authoritative, never the arrival time
    first = Punch.objects.order_by("punched_at").first()
    assert first.punched_at == datetime(2026, 3, 2, 8, tzinfo=LAGOS)
    assert first.has_flag("late_sync")
    assert Device.objects.get(serial_number=sim.serial).attlog_stamp == "3000"
    # attendance recomputed for every affected date, not just today
    days = set(AttendanceDay.objects.filter(enrollee=enrollee)
               .values_list("work_date", flat=True))
    assert min(days) == date(2026, 3, 2) and max(days) >= date(2026, 3, 11)
    assert backlog_events and sum(e["count"] for e in backlog_events) == 3000


def test_partial_upload_failure_then_retry(simulator):
    sim = simulator()
    sim.go_offline()
    for i in range(1200):
        sim.punch(str(i % 20), local(2026, 4, 1) + timedelta(minutes=i))
    sim.go_online(catch_up=False)
    from fingerprint_attendance.adms import views

    original = views.ingest_punches
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database went away mid-backlog")
        return original(*args, **kwargs)

    with mock.patch.object(views, "ingest_punches", side_effect=flaky):
        responses = sim.upload_pending()
    assert [r.status for r in responses] == [200, 500]
    assert Punch.objects.count() == 500
    assert Device.objects.get(serial_number=sim.serial).attlog_stamp == "500"
    sim.go_online()  # the device retries after reconnecting
    assert Punch.objects.count() == 1200 and sim.pending() == []


def test_duplicate_resend_after_cursor_reset(simulator, admin_user):
    sim = simulator()
    for i in range(50):
        sim.punch("1", local(2026, 4, 2, 8) + timedelta(minutes=10 * i))
    assert Punch.objects.count() == 50
    device = Device.objects.get(serial_number=sim.serial)
    services.reset_upload_cursor(device, ("ATTLOG",), by=admin_user)
    assert sim.handshake()["ATTLOGStamp"] == "0"
    responses = sim.upload_pending()
    assert responses and all(r.status == 200 for r in responses)
    assert Punch.objects.count() == 50  # dedupe made the full re-send safe


def test_reupload_from_date_queues_query(simulator, admin_user):
    sim = simulator()
    sim.punch("1", local(2026, 4, 2, 8))
    device = Device.objects.get(serial_number=sim.serial)
    commands = services.reset_upload_cursor(device, ("ATTLOG",),
                                            from_datetime=timezone.now() - timedelta(days=30),
                                            by=admin_user)
    assert commands[0].command_type == "query_attlog"
    sim.run_commands()
    assert any(c.startswith("DATA QUERY ATTLOG StartTime=") for c in sim.executed)


def test_enrollment_while_device_offline(simulator, make_enrollee):
    sim = simulator()
    sim.go_offline()
    enrollee = make_enrollee(pin="300")
    services.store_template(enrollee, 6, "10", b"\x05" * 256, source="import")
    services.store_template(enrollee, 7, "10", b"\x06" * 256, source="import")
    sim.go_online()
    sim.run_commands()
    assert sim.templates[("300", 6)] == b"\x05" * 256
    assert sim.templates[("300", 7)] == b"\x06" * 256


def test_backlog_across_midnight_and_month(simulator, make_enrollee, fpa, backlog_events):
    fpa(DEFAULT_SCHEDULE={"start": "22:00", "end": "06:00"}, DAY_BOUNDARY_RESOLVER="shift_aware")
    enrollee = make_enrollee(pin="400")
    sim = simulator()
    sim.go_offline()
    sim.punch("400", local(2026, 1, 31, 21, 58))
    sim.punch("400", local(2026, 2, 1, 6, 3))   # night shift ends in the next month
    sim.punch("400", local(2026, 2, 1, 21, 59))
    sim.punch("400", local(2026, 2, 2, 6, 1))
    sim.go_online()
    days = {d.work_date: d for d in AttendanceDay.objects.filter(enrollee=enrollee)}
    assert set(days) == {date(2026, 1, 31), date(2026, 2, 1)}
    assert days[date(2026, 1, 31)].worked_duration == timedelta(hours=8, minutes=5)
    event = backlog_events[-1]
    assert event["start_date"] == date(2026, 1, 31) and event["end_date"] == date(2026, 2, 1)


def test_clock_drift_detection_and_auto_correction(make_device, fpa):
    fpa(AUTO_CORRECT_DEVICE_TIME=True, CLOCK_DRIFT_WARNING_SECONDS=60,
        MAX_AUTO_TIME_CORRECTION="1h")
    device = make_device(timezone="UTC")
    drifts = []
    device_clock_drift.connect(lambda **kw: drifts.append(kw["drift_seconds"]), weak=False,
                               dispatch_uid="t-drift")
    try:
        now_utc = timezone.now().replace(tzinfo=None)
        services.record_clock_drift(device, now_utc + timedelta(minutes=5))
        assert DeviceCommand.objects.filter(command_type="set_time").count() == 1
        services.record_clock_drift(device, now_utc + timedelta(hours=5))  # too large
        assert DeviceCommand.objects.filter(command_type="set_time").count() == 1
        services.record_clock_drift(device, now_utc)  # fine
    finally:
        device_clock_drift.disconnect(dispatch_uid="t-drift")
    assert len(drifts) == 2 and 290 < drifts[0] < 310


def test_punches_flagged_while_clock_drifts(simulator, fpa):
    fpa(CLOCK_DRIFT_WARNING_SECONDS=60)
    sim = simulator()
    Device.objects.filter(serial_number=sim.serial).update(clock_drift_seconds=900)
    sim.punch("1", local(2026, 4, 2, 8))
    assert Punch.objects.get().has_flag("clock_drift")


def test_clear_logs_requires_reconciliation(simulator, admin_user):
    sim = simulator()
    device = Device.objects.get(serial_number=sim.serial)
    with pytest.raises(UnsafeOperation, match="not reported"):
        services.clear_device_logs(device, by=admin_user)
    sim.go_offline()
    for i in range(5):
        sim.punch("1", local(2026, 4, 3, 8, i * 5))
    sim.online = True
    sim.poll()  # INFO says 5 logs on the device, server has 0
    device.refresh_from_db()
    with pytest.raises(UnsafeOperation) as exc:
        services.clear_device_logs(device, by=admin_user)
    assert exc.value.details["pending_upload_estimate"] == 5
    sim.upload_pending()
    device.refresh_from_db()
    command = services.clear_device_logs(device, by=admin_user)
    assert command.command_type == "clear_logs"
    sim.run_commands()
    device.refresh_from_db()
    assert device.log_count_baseline == 5 and device.reported_punch_count == 0
    # new punches after the clear reconcile against the baseline
    sim.punch("1", local(2026, 4, 3, 17))
    sim.poll()
    device.refresh_from_db()
    report = {r["serial_number"]: r for r in services.reconciliation_report()}[sim.serial]
    assert report["pending_upload_estimate"] == 0 and report["server_count_since_last_clear"] \
        == 1


def test_clear_data_resyncs_device(simulator, make_enrollee, admin_user):
    enrollee = make_enrollee(pin="500")
    sim = simulator()
    services.store_template(enrollee, 1, "10", b"x" * 40, source="import")
    sim.run_commands()
    device = Device.objects.get(serial_number=sim.serial)
    sim.poll()
    device.refresh_from_db()
    services.clear_device_data(device, by=admin_user)
    sim.run_commands()  # CLEAR DATA, then the automatic full resync
    assert ("500", 1) in sim.templates


def test_capacity_warning(simulator, fpa):
    from fingerprint_attendance.signals import device_log_capacity_warning

    fpa(DEFAULT_LOG_CAPACITY=10, LOG_CAPACITY_WARNING_RATIO=0.5)
    sim = simulator(realtime=False)
    warnings = []
    device_log_capacity_warning.connect(lambda **kw: warnings.append(kw["used"]), weak=False,
                                        dispatch_uid="t-cap")
    try:
        for i in range(6):
            sim.punch("1", local(2026, 4, 4, 8, i))
        sim.poll()
        sim.poll()  # edge triggered: warned once
    finally:
        device_log_capacity_warning.disconnect(dispatch_uid="t-cap")
    assert warnings == [6]
    health = services.device_health(Device.objects.get(serial_number=sim.serial))
    assert health["log_capacity_warning"] and health["pending_upload_estimate"] == 6


def test_health_endpoint_data(simulator, make_device):
    sim = simulator()
    sim.punch("1", local(2026, 4, 5, 8))
    offline = make_device(last_seen_at=timezone.now() - timedelta(hours=3),
                          connection_state="online")
    services.queue_command(offline, "check", {})
    assert services.mark_offline_devices() == 1
    summary = services.health_summary()
    assert summary["devices"]["total"] == 2 and summary["devices"]["online"] == 1
    rows = {r["serial_number"]: r for r in summary["device_health"]}
    assert rows[offline.serial_number]["offline_seconds"] > 3 * 3600 - 5
    assert rows[offline.serial_number]["pending_commands"] == 1
    assert rows[sim.serial]["last_punch_at"] is not None
