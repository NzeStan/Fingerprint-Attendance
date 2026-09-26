"""Pull mode: fake adapter scenarios and the pyzk adapter against a mocked ``zk`` module."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command

from fingerprint_attendance import services
from fingerprint_attendance.models import Device, Punch
from fingerprint_attendance.pull.base import PulledPunch, PullError, adapter_for
from fingerprint_attendance.pull.service import (
    LiveCapture,
    import_attendance,
    poll_forever,
    sync_device,
)
from tests.conftest import local

pytestmark = pytest.mark.django_db
FAKE = "fingerprint_attendance.testing.FakePullAdapter"


@pytest.fixture
def pull_device(make_device, fpa):
    fpa(PULL_ENABLED=True, PULL_ADAPTER=FAKE)
    return make_device("PULL1", mode="pull", pull_host="10.0.0.9", timezone="Africa/Lagos")


def terminal(device):
    return adapter_for(device).terminal


def test_import_uses_marker_and_same_pipeline(pull_device):
    term = terminal(pull_device)
    term.punches = [PulledPunch("1", local(2026, 1, 5, 8), status="0"),
                    PulledPunch("1", local(2026, 1, 5, 17), status="1")]
    term.clock = datetime.now()
    result = import_attendance(pull_device)
    assert result.created_count == 2
    pull_device.refresh_from_db()
    assert pull_device.pull_last_record_at == Punch.objects.latest("punched_at").punched_at
    assert Punch.objects.filter(source="pull").count() == 2
    assert pull_device.reported_punch_count == 2 and pull_device.clock_drift_seconds is not None
    term.punches.append(PulledPunch("1", local(2026, 1, 6, 8)))
    assert import_attendance(pull_device).created_count == 1
    full = import_attendance(pull_device, full=True)
    assert full.created_count == 0 and full.duplicates == 3


def test_connection_failure(pull_device):
    terminal(pull_device).fail_connects = 1
    with pytest.raises(PullError):
        import_attendance(pull_device)


def test_sync_device(pull_device, make_enrollee):
    enrollee = make_enrollee(pin="7")
    services.store_template(enrollee, 3, "10", b"\x03" * 64, source="import", fan_out=False)
    term = terminal(pull_device)
    from fingerprint_attendance.pull.base import PulledUser

    term.users["999"] = PulledUser("999", "stranger")
    result = sync_device(pull_device, remove_extra=True)
    assert result == {"users": 1, "templates": 1, "deleted": 1}
    assert term.templates[("7", 3)].template == b"\x03" * 64
    assert "999" not in term.users
    assert sync_device(pull_device) == {"users": 0, "templates": 0, "deleted": 0}


def test_live_capture_gap_fill_after_reconnect(pull_device):
    term = terminal(pull_device)
    term.punches = [PulledPunch("1", local(2026, 1, 5, 8))]
    term.live_queue = [None, PulledPunch("1", local(2026, 1, 5, 12))]
    sleeps = []
    capture = LiveCapture(pull_device, timeout=1, sleep=sleeps.append)
    # cycle 1: gap-fill (08:00) + live (12:00) then disconnect; punches made while down:
    original_drain = term.live_queue

    def refill(*_):
        term.punches.append(PulledPunch("1", local(2026, 1, 5, 13)))
        term.fail_connects = 0

    capture.sleep = lambda s: (sleeps.append(s), refill())
    capture.run(max_cycles=2)
    times = sorted(p.device_local_time for p in Punch.objects.all())
    assert times == ["2026-01-05 08:00:00", "2026-01-05 12:00:00", "2026-01-05 13:00:00"]
    assert sleeps and sleeps[0] == 1.0
    assert original_drain == []


def test_live_capture_stop(pull_device):
    capture = LiveCapture(pull_device)
    capture.stop()
    capture.run()
    capture.install_signal_handlers()


def test_poll_forever(pull_device):
    terminal(pull_device).punches = [PulledPunch("2", local(2026, 1, 5, 8))]
    other = Device.objects.create(serial_number="PULL2", mode="pull", status="active")
    terminal(other).fail_connects = 5
    poll_forever([pull_device, other], interval=timedelta(0), max_rounds=1)
    assert Punch.objects.count() == 1


def test_pull_commands(pull_device, make_enrollee):
    terminal(pull_device).punches = [PulledPunch("3", local(2026, 1, 5, 8))]
    out = StringIO()
    call_command("fpa_pull_attendance", "--device", "PULL1", stdout=out)
    assert "1 new" in out.getvalue()
    call_command("fpa_pull_attendance", "--full", stdout=out)
    terminal(pull_device).fail_connects = 1
    with pytest.raises(CommandError, match="failed"):
        call_command("fpa_pull_attendance", stdout=out, stderr=StringIO())
    with pytest.raises(CommandError, match="unknown"):
        call_command("fpa_pull_attendance", "--device", "NOPE", stdout=out)
    call_command("fpa_sync_device", "--device", "PULL1", stdout=out)
    assert "users" in out.getvalue()
    with mock.patch("fingerprint_attendance.pull.service.poll_forever") as loop:
        call_command("fpa_pull_attendance", "--loop", stdout=out)
    loop.assert_called_once()


def test_pull_commands_require_enabled(fpa, make_device):
    fpa(PULL_ENABLED=False)
    with pytest.raises(CommandError, match="PULL_ENABLED"):
        call_command("fpa_pull_attendance")
    with pytest.raises(CommandError, match="PULL_ENABLED"):
        call_command("fpa_live_capture")
    fpa(PULL_ENABLED=True, PULL_ADAPTER=FAKE)
    with pytest.raises(CommandError, match="no active"):
        call_command("fpa_pull_attendance")
    with pytest.raises(CommandError, match="no active"):
        call_command("fpa_live_capture")


def test_live_capture_command(pull_device):
    terminal(pull_device).live_queue = []
    with mock.patch("fingerprint_attendance.pull.service.LiveCapture.run"), \
            mock.patch("signal.signal"):
        out = StringIO()
        call_command("fpa_live_capture", "--device", "PULL1", stdout=out)
    assert "stopped" in out.getvalue()


# --------------------------------------------------------------------------- pyzk adapter


class FakeZKConn:
    def __init__(self):
        self.users = [types.SimpleNamespace(uid=1, user_id="10", name="Ada", privilege=0)]
        self.calls = []
        self.records, self.fingers, self.rec_cap = 5, 2, 1000

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return
        return call

    def get_users(self):
        return self.users

    def get_templates(self):
        return [types.SimpleNamespace(uid=1, fid=6, valid=1, template=b"\x01\x02"),
                types.SimpleNamespace(uid=9, fid=1, valid=1, template=b"")]

    def get_attendance(self):
        return [types.SimpleNamespace(user_id="10", timestamp=datetime(2026, 1, 5, 8), punch=0,
                                      status=1)]

    def get_serialnumber(self):
        return "ZK123"

    def get_firmware_version(self):
        return "Ver 6.60"

    def get_platform(self):
        raise RuntimeError("unsupported")

    def get_time(self):
        return datetime(2026, 1, 5, 8)

    def live_capture(self, new_timeout=10):
        yield None
        yield types.SimpleNamespace(user_id="10", timestamp=datetime(2026, 1, 5, 9), punch=1,
                                    status=None)


@pytest.fixture
def fake_zk(monkeypatch):
    conn = FakeZKConn()
    zk_module = types.ModuleType("zk")

    class ZK:
        def __init__(self, ip, **kwargs):
            self.kwargs = kwargs

        def connect(self):
            if ip_fail[0]:
                raise OSError("unreachable")
            return conn

    ip_fail = [False]
    zk_module.ZK = ZK
    finger_mod = types.ModuleType("zk.finger")
    finger_mod.Finger = lambda uid, fid, valid, template: ("finger", uid, fid, template)
    user_mod = types.ModuleType("zk.user")
    user_mod.User = lambda *a: ("user", *a)
    monkeypatch.setitem(sys.modules, "zk", zk_module)
    monkeypatch.setitem(sys.modules, "zk.finger", finger_mod)
    monkeypatch.setitem(sys.modules, "zk.user", user_mod)
    return conn, ip_fail


def test_pyzk_adapter(make_device, fake_zk):
    from fingerprint_attendance.pull.base import PulledTemplate, PulledUser
    from fingerprint_attendance.pull.pyzk_adapter import PyZKPullAdapter

    conn, ip_fail = fake_zk
    device = make_device(pull_host="10.0.0.5", pull_comm_key=123)
    with PyZKPullAdapter(device) as adapter:
        info = adapter.get_info()
        assert info.serial_number == "ZK123" and info.punch_count == 5
        assert info.platform == "" and info.device_time == datetime(2026, 1, 5, 8)
        assert adapter.get_users()[0].pin == "10"
        templates = adapter.get_templates()
        assert len(templates) == 1 and templates[0].pin == "10" and templates[0].finger_index == 6
        punch = adapter.get_attendance()[0]
        assert punch.pin == "10" and punch.status == "0" and punch.verify == 1
        adapter.set_user(PulledUser("11", "New"))
        adapter.set_templates(PulledUser("11", "New"), [PulledTemplate("11", 2, b"x")])
        adapter.delete_user("10")
        adapter.set_time(datetime(2026, 1, 5, 8))
        live = list(adapter.live_capture(timeout=1))
        assert live[0] is None and live[1].pin == "10"
    names = [c[0] for c in conn.calls]
    assert {"set_user", "save_user_template", "delete_user", "set_time", "disconnect",
            "disable_device", "enable_device"} <= set(names)
    ip_fail[0] = True
    with pytest.raises(PullError, match="cannot connect"):
        PyZKPullAdapter(device).connect()
    with pytest.raises(PullError, match="no pull_host"):
        PyZKPullAdapter(make_device()).connect()
