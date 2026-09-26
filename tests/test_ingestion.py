"""Punch ingestion: timezone, dedupe (exact + window), anomalies, state resolvers, bulk."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from django.utils import timezone

from fingerprint_attendance import services
from fingerprint_attendance.ingestion.candidates import PunchCandidate
from fingerprint_attendance.ingestion.pipeline import ingest_punches
from fingerprint_attendance.models import Punch, PunchAdjustment
from fingerprint_attendance.signals import backlog_synced, punch_flagged, punch_received
from tests.conftest import local

pytestmark = pytest.mark.django_db


def cand(pin, when, device=None, **kw):
    return PunchCandidate(pin=pin, local_time=when, device=device, **kw)


def test_timezone_conversion_per_device(make_device):
    lagos = make_device(timezone="Africa/Lagos")
    ny = make_device(timezone="America/New_York")
    default = make_device()
    ingest_punches([cand("1", local(2026, 7, 1, 9), lagos), cand("1", local(2026, 7, 1, 9), ny),
                    cand("1", local(2026, 7, 1, 9), default)])
    times = {p.device_id: p.punched_at for p in Punch.objects.all()}
    assert times[lagos.pk] == datetime(2026, 7, 1, 8, tzinfo=dt_tz.utc)
    assert times[ny.pk] == datetime(2026, 7, 1, 13, tzinfo=dt_tz.utc)  # EDT
    assert times[default.pk] == datetime(2026, 7, 1, 8, tzinfo=dt_tz.utc)  # TIME_ZONE Lagos


def test_invalid_device_timezone_falls_back(make_device):
    device = make_device(timezone="Nowhere/City")
    ingest_punches([cand("1", local(2026, 1, 1, 9), device)])
    assert Punch.objects.get().punched_at.hour == 8


def test_dst_ambiguous_time(make_device):
    device = make_device(timezone="America/New_York")
    ingest_punches([cand("1", local(2026, 11, 1, 1, 30), device)])  # repeated hour
    assert Punch.objects.get().punched_at == datetime(2026, 11, 1, 5, 30, tzinfo=dt_tz.utc)


def test_exact_duplicates_dropped(make_device):
    device = make_device()
    first = ingest_punches([cand("1", local(2026, 1, 5, 8), device),
                            cand("1", local(2026, 1, 5, 8), device)])
    assert first.created_count == 1 and first.duplicates == 1
    again = ingest_punches([cand("1", local(2026, 1, 5, 8), device)])
    assert again.created_count == 0 and again.duplicates == 1


def test_soft_duplicates_flagged_or_skipped(make_device, fpa):
    device = make_device()
    result = ingest_punches([cand("1", local(2026, 1, 5, 8, 0, 0), device),
                             cand("1", local(2026, 1, 5, 8, 0, 30), device),
                             cand("1", local(2026, 1, 5, 8, 5, 0), device)])
    assert result.created_count == 3 and result.soft_duplicates == 1
    flagged = Punch.objects.with_flag("duplicate").get()
    assert flagged.device_local_time.endswith("08:00:30")
    assert Punch.objects.effective().count() == 2
    ingest_punches([cand("1", local(2026, 1, 5, 8, 5, 20), device)])  # vs stored punch
    assert Punch.objects.with_flag("duplicate").count() == 2
    fpa(DEDUPE_WINDOW_ACTION="skip")
    result = ingest_punches([cand("1", local(2026, 1, 5, 8, 5, 40), device)])
    assert result.created_count == 0 and result.soft_duplicates == 1
    fpa(DEDUPE_WINDOW_SECONDS=0)
    assert ingest_punches([cand("1", local(2026, 1, 5, 8, 5, 41), device)]).created_count == 1


def test_custom_dedupe_key(make_device, fpa):
    fpa(DEDUPE_KEY_BUILDER="tests.testapp.hooks.custom_dedupe_key", DEDUPE_WINDOW_SECONDS=0)
    a, b = make_device(), make_device()
    result = ingest_punches([cand("1", local(2026, 1, 5, 8), a),
                             cand("1", local(2026, 1, 5, 8), b)])
    assert result.created_count == 1


def test_anomaly_flags(make_device, make_enrollee, fpa):
    fpa(LATE_SYNC_THRESHOLD="10m", FUTURE_PUNCH_TOLERANCE="5m", CLOCK_DRIFT_WARNING_SECONDS=60)
    device = make_device(timezone="UTC", clock_drift_seconds=300)
    make_enrollee(pin="7")
    now = timezone.now().replace(microsecond=0)
    naive = lambda d: d.replace(tzinfo=None)  # noqa: E731
    ingest_punches([
        cand("7", naive(now - timedelta(hours=2)), device),
        cand("7", naive(now + timedelta(hours=1)), device),
        cand("999", naive(now), device),
    ])
    late = Punch.objects.get(punched_at=now - timedelta(hours=2))
    assert set(late.flag_list) == {"late_sync", "clock_drift"}
    future = Punch.objects.get(punched_at=now + timedelta(hours=1))
    assert "future" in future.flag_list
    unknown = Punch.objects.get(raw_pin="999")
    assert "unknown_pin" in unknown.flag_list and unknown.enrollee is None


def test_unknown_pins_rejected_when_configured(make_device, fpa):
    fpa(ACCEPT_UNKNOWN_PIN_PUNCHES=False)
    result = ingest_punches([cand("404", local(2026, 1, 5, 8), make_device())])
    assert result.created_count == 0 and result.rejected[0][1] == "unknown pin"


def test_candidate_without_time_rejected(make_device):
    result = ingest_punches([PunchCandidate(pin="1", device=make_device())])
    assert result.rejected[0][1] == "no time"


@pytest.mark.parametrize(("resolver", "expected"), [
    ("trust_device", ["check_in", "check_out", "break_out", "unknown"]),
    ("alternate", ["check_in", "check_out", "check_in", "check_out"]),
    ("first_last", ["check_in", "check_out", "check_out", "check_out"]),
])
def test_state_resolvers(make_device, make_enrollee, fpa, resolver, expected):
    fpa(PUNCH_STATE_RESOLVER=resolver)
    device = make_device()
    make_enrollee(pin="5")
    ingest_punches([cand("5", local(2026, 1, 5, 8), device, raw_state="0"),
                    cand("5", local(2026, 1, 5, 12), device, raw_state="1")])
    ingest_punches([cand("5", local(2026, 1, 5, 13), device, raw_state="2"),
                    cand("5", local(2026, 1, 5, 17), device, raw_state="9")])
    states = list(Punch.objects.order_by("punched_at").values_list("state", flat=True))
    assert states == expected


def test_custom_state_map(make_device, fpa):
    fpa(PUNCH_STATE_MAP={"0": "check_out"})
    ingest_punches([cand("5", local(2026, 1, 5, 8), make_device(), raw_state="0")])
    assert Punch.objects.get().state == "check_out"


def test_signals_and_backlog(make_device, fpa):
    fpa(BACKLOG_SIGNAL_THRESHOLD=3)
    device = make_device()
    got = {"single": 0, "flagged": 0, "backlog": []}
    punch_received.connect(lambda **kw: got.__setitem__("single", got["single"] + 1),
                           weak=False, dispatch_uid="t1")
    punch_flagged.connect(lambda **kw: got.__setitem__("flagged", got["flagged"] + 1),
                          weak=False, dispatch_uid="t2")
    backlog_synced.connect(lambda **kw: got["backlog"].append((kw["count"], kw["start_date"],
                                                                kw["end_date"])),
                           weak=False, dispatch_uid="t3")
    try:
        ingest_punches([cand(str(i), local(2026, 1, 5 + i, 8), device) for i in range(3)])
    finally:
        for uid in ("t1", "t2", "t3"):
            for sig in (punch_received, punch_flagged, backlog_synced):
                sig.disconnect(dispatch_uid=uid)
    assert got["single"] == 3 and got["flagged"] == 3  # unknown pins are flagged
    assert got["backlog"][0][0] == 3


def test_event_batch_limit(make_device, fpa):
    fpa(PUNCH_EVENT_BATCH_LIMIT=2)
    calls = []
    punch_received.connect(lambda **kw: calls.append(1), weak=False, dispatch_uid="t-limit")
    try:
        ingest_punches([cand(str(i), local(2026, 1, 5, 8, i), make_device()) for i in range(5)])
    finally:
        punch_received.disconnect(dispatch_uid="t-limit")
    assert calls == []


def test_bulk_ingest_is_efficient(make_device, make_enrollee, django_assert_max_num_queries):
    device = make_device()
    make_enrollee(pin="1")
    candidates = [cand("1", local(2026, 1, 1) + timedelta(minutes=5 * i), device)
                  for i in range(2000)]
    with django_assert_max_num_queries(60):
        result = ingest_punches(candidates, process=False, emit_events=False)
    assert result.created_count == 2000
    device.refresh_from_db()
    assert device.last_punch_at == Punch.objects.latest("punched_at").punched_at


def test_manual_punch_and_import(make_enrollee, make_device, admin_user):
    enrollee = make_enrollee()
    punch = services.create_manual_punch(enrollee, "2026-01-05T08:00:00+01:00",
                                         state="check_in", by=admin_user, note="forgot")
    assert punch.source == "manual" and punch.has_flag("manual") and punch.state == "check_in"
    assert punch.created_by == admin_user
    from fingerprint_attendance.exceptions import InvalidInput

    with pytest.raises(InvalidInput, match="identical"):
        services.create_manual_punch(enrollee, "2026-01-05T08:00:00+01:00")
    with pytest.raises(InvalidInput, match="state"):
        services.create_manual_punch(enrollee, "2026-01-05T09:00:00+01:00", state="teleport")
    device = make_device("IMP1", timezone="UTC")
    result = services.import_punches([
        {"pin": "1", "punched_at": "2026-01-06 08:00:00", "device_serial": "IMP1",
         "state": "0"},
        {"pin": "1", "punched_at": "2026-01-06T09:00:00Z"},
        {"pin": "1", "punched_at": "garbage"},
        {"pin": "1", "punched_at": "2026-01-06 08:00:00", "device_serial": "NOPE"},
    ], by=admin_user)
    assert result.created_count == 2 and len(result.rejected) == 2
    imported = Punch.objects.get(device=device)
    assert imported.punched_at == datetime(2026, 1, 6, 8, tzinfo=dt_tz.utc)
    with pytest.raises(InvalidInput):
        services.import_punches([], source="teleport")


def test_adjustments_never_modify_raw(make_enrollee, admin_user):
    enrollee = make_enrollee()
    punch = services.create_manual_punch(enrollee, "2026-01-05T08:00:00+01:00")
    adj = services.adjust_punch(punch, action="set_state", new_state="check_out",
                                reason="wrong key", by=admin_user)
    punch.refresh_from_db()
    assert punch.state == "unknown" and adj.new_state == "check_out"
    from fingerprint_attendance.exceptions import InvalidInput

    with pytest.raises(InvalidInput):
        services.adjust_punch(punch, action="explode")
    with pytest.raises(InvalidInput):
        services.adjust_punch(punch, action="set_time")
    with pytest.raises(InvalidInput):
        services.adjust_punch(punch, action="set_state", new_state="nope")
    services.adjust_punch(punch, action="set_time", new_punched_at="2026-01-05T09:00:00+01:00")
    assert PunchAdjustment.objects.count() == 2


def test_cross_source_dedupe(make_device):
    """The same punch arriving via ADMS and via pull import is stored once."""
    device = make_device()
    ingest_punches([cand("1", local(2026, 1, 5, 8), device, source="adms")])
    result = ingest_punches([cand("1", local(2026, 1, 5, 8), device, source="pull")])
    assert result.created_count == 0
