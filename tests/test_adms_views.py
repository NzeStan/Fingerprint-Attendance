"""ADMS endpoints end-to-end through the device simulator and raw HTTP."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
from django.test import Client

from fingerprint_attendance import services
from fingerprint_attendance.constants import ConnectionState, DeviceStatus, EventLogType
from fingerprint_attendance.models import Device, DeviceEventLog, Punch
from fingerprint_attendance.testing import ADMSDeviceSimulator
from tests.conftest import local

pytestmark = pytest.mark.django_db


def raw_post(client, path, body, **params):
    from urllib.parse import urlencode

    return client.post(f"{path}?{urlencode(params)}", data=body.encode(),
                       content_type="text/plain")


def test_handshake_auto_registers_pending(db):
    sim = ADMSDeviceSimulator(Client(), serial="NEW001")
    options = sim.handshake()
    device = Device.objects.get(serial_number="NEW001")
    assert device.status == DeviceStatus.PENDING_APPROVAL
    assert device.connection_state == ConnectionState.ONLINE
    assert device.push_version == "2.2.14" and device.last_handshake_at is not None
    assert options["ATTLOGStamp"] == "None" and options["Realtime"] == "1"


def test_pending_device_data_is_refused_then_accepted_after_approval(db):
    sim = ADMSDeviceSimulator(Client(), serial="PEND01")
    sim.handshake()
    sim.punch("1", local(2026, 1, 5, 8))
    assert sim.uploaded == 0 and Punch.objects.count() == 0  # 403 -> device keeps data
    assert sim.poll() == []  # no commands for pending devices
    services.approve_device(Device.objects.get(serial_number="PEND01"))
    sim.go_online()
    assert Punch.objects.count() == 1 and sim.pending() == []


def test_accept_from_pending_devices_flags_punches(db, fpa):
    fpa(ADMS_ACCEPT_FROM_PENDING_DEVICES=True)
    sim = ADMSDeviceSimulator(Client(), serial="PEND02")
    sim.handshake()
    sim.punch("1", local(2026, 1, 5, 8))
    assert Punch.objects.get().has_flag("pending_device")


def test_auto_register_disabled(db, fpa):
    fpa(ADMS_AUTO_REGISTER_DEVICES=False)
    response = Client().get("/iclock/cdata", {"SN": "X1"})
    assert response.status_code == 403
    assert not Device.objects.exists()


def test_no_approval_required(db, fpa):
    fpa(ADMS_REQUIRE_DEVICE_APPROVAL=False)
    Client().get("/iclock/cdata", {"SN": "OPEN1"})
    assert Device.objects.get().status == DeviceStatus.ACTIVE


def test_disabled_device_rejected(make_device):
    device = make_device("DIS1", status=DeviceStatus.DISABLED)
    assert Client().get("/iclock/cdata", {"SN": device.serial_number}).status_code == 403


def test_missing_serial_and_disabled_adms(db, fpa):
    assert Client().get("/iclock/cdata").status_code == 400
    fpa(ADMS_ENABLED=False)
    assert Client().get("/iclock/cdata", {"SN": "A"}).status_code == 404


def test_token_required(make_device, fpa):
    fpa(ADMS_DEVICE_TOKEN_REQUIRED=True, ADMS_AUTO_REGISTER_DEVICES=False)
    device = make_device("TOK1")
    token = services.issue_device_token(device)
    client = Client()
    assert client.get("/iclock/cdata", {"SN": "TOK1"}).status_code == 403
    assert client.get("/iclock/cdata", {"SN": "TOK1", "token": "wrong"}).status_code == 403
    assert client.get("/iclock/cdata", {"SN": "TOK1", "token": token}).status_code == 200
    assert client.get("/iclock/cdata", {"SN": "TOK1"},
                      HTTP_X_FPA_DEVICE_TOKEN=token).status_code == 200
    assert client.get("/iclock/cdata", {"SN": "TOK1", "pushcommkey": token}).status_code == 200
    assert DeviceEventLog.objects.filter(event_type=EventLogType.AUTH_FAILED).count() == 2
    # unknown devices cannot auto-register when tokens are required
    fpa(ADMS_DEVICE_TOKEN_REQUIRED=True, ADMS_AUTO_REGISTER_DEVICES=True)
    assert client.get("/iclock/cdata", {"SN": "NEWTOK"}).status_code == 403


def test_ip_allowlist_and_trusted_proxy(make_device, fpa):
    make_device("IP1")
    fpa(ADMS_ALLOWED_IPS=["10.0.0.0/8"])
    client = Client()
    assert client.get("/iclock/cdata", {"SN": "IP1"}, REMOTE_ADDR="192.168.1.2"
                      ).status_code == 403
    assert client.get("/iclock/cdata", {"SN": "IP1"}, REMOTE_ADDR="10.1.2.3").status_code == 200
    fpa(ADMS_ALLOWED_IPS=["10.0.0.0/8"], ADMS_TRUSTED_PROXY_IPS=["127.0.0.1"])
    assert client.get("/iclock/cdata", {"SN": "IP1"}, REMOTE_ADDR="127.0.0.1",
                      HTTP_X_FORWARDED_FOR="10.9.9.9").status_code == 200
    assert client.get("/iclock/cdata", {"SN": "IP1"}, REMOTE_ADDR="127.0.0.1",
                      HTTP_X_FORWARDED_FOR="8.8.8.8").status_code == 403
    assert Device.objects.get(serial_number="IP1").last_ip == "10.9.9.9"


def test_rate_limit_and_size_limit(make_device, fpa):
    make_device("RL1")
    fpa(ADMS_RATE_LIMIT="2/m")
    client = Client()
    codes = [client.get("/iclock/ping", {"SN": "RL1"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    fpa(ADMS_MAX_REQUEST_BYTES=10, ADMS_RATE_LIMIT=None)
    response = raw_post(client, "/iclock/cdata", "x" * 100, SN="RL1", table="ATTLOG")
    assert response.status_code == 413


def test_attlog_upload_and_cursor(simulator):
    sim = simulator()
    sim.punch("1001", local(2026, 1, 5, 8, 0))
    sim.punch("1001", local(2026, 1, 5, 17, 0))
    device = Device.objects.get(serial_number=sim.serial)
    assert device.attlog_stamp == "2"
    assert Punch.objects.count() == 2
    punch = Punch.objects.order_by("punched_at").first()
    assert punch.punched_at.isoformat() == "2026-01-05T07:00:00+00:00"  # Lagos is UTC+1
    assert punch.device_local_time == "2026-01-05 08:00:00"
    assert punch.source == "adms" and punch.state == "check_in"
    assert punch.has_flag("unknown_pin")
    assert sim.handshake()["ATTLOGStamp"] == "2"


def test_bad_lines_do_not_reject_batch(simulator):
    sim = simulator()
    for minute in range(3):
        sim.punch("1001", local(2026, 1, 5, 8, minute * 5), upload=False)
    responses = sim.upload_pending(corrupt_line=1)
    assert responses[0].status == 200 and responses[0].text == "OK: 3"
    assert Punch.objects.count() == 2
    assert DeviceEventLog.objects.filter(event_type=EventLogType.PARSE_ERROR).count() == 1
    assert sim.pending() == []  # the device will not resend the batch forever


def test_database_failure_means_resend(simulator):
    sim = simulator()
    sim.punch("1001", local(2026, 1, 5, 8, 0), upload=False)
    with mock.patch("fingerprint_attendance.adms.views.ingest_punches",
                    side_effect=RuntimeError("db down")):
        responses = sim.upload_pending()
    assert responses[0].status == 500
    device = Device.objects.get(serial_number=sim.serial)
    assert device.attlog_stamp == "" and sim.pending()  # cursor not advanced
    sim.upload_pending()
    assert Punch.objects.count() == 1


def test_attlog_fixture_variants(simulator):
    from pathlib import Path

    sim = simulator()
    for name in ("attlog_legacy.txt", "attlog_push2.txt", "attlog_push3.txt"):
        body = (Path(__file__).parent / "fixtures" / "adms" / name).read_text()
        response = sim.post("cdata", body, table="ATTLOG", Stamp=name)
        assert response.status == 200
    # 3 + 3 + 2 lines; the 08:01:02 punch of 1001 appears in all three -> deduplicated
    assert Punch.objects.filter(raw_pin="1001", device_local_time="2026-01-05 08:01:02"
                                ).count() == 1


def test_getrequest_and_devicecmd(simulator):
    sim = simulator(firmware="biodata")
    device = Device.objects.get(serial_number=sim.serial)
    cmd = services.queue_command(device, "reboot", {})
    commands = sim.poll()
    assert commands == [(cmd.pk, "REBOOT")]
    cmd.refresh_from_db()
    assert cmd.status == "sent" and cmd.attempts == 1
    sim.ack([(cmd.pk, 0, "REBOOT", "")])
    cmd.refresh_from_db()
    assert cmd.status == "succeeded"
    # INFO updates device counters
    services.queue_command(device, "info", {})
    sim.run_commands()
    device.refresh_from_db()
    assert device.model_name == "SIM-biodata" and device.log_capacity == 100000
    assert device.fp_algorithm_version == "10"


def test_getrequest_info_param_updates_counts(simulator):
    sim = simulator()
    sim.log.extend([])
    sim.users["1"] = {"name": "x", "privilege": 0}
    sim.poll()
    device = Device.objects.get(serial_number=sim.serial)
    assert device.reported_user_count == 1 and device.reported_punch_count == 0
    assert device.firmware_version == "Ver 8.0.4-SIM"


def test_options_table(simulator):
    from pathlib import Path

    sim = simulator()
    body = (Path(__file__).parent / "fixtures" / "adms" / "options.txt").read_text()
    assert sim.post("cdata", body, table="options").status == 200
    device = Device.objects.get(serial_number=sim.serial)
    assert device.model_name == "SpeedFace-V5L" and device.fp_algorithm_version == "12"


def test_unknown_table_and_attphoto_acknowledged(simulator):
    sim = simulator()
    assert sim.post("cdata", "blob", table="WHATEVER").text == "OK"
    assert sim.post("cdata", "jpeg", table="ATTPHOTO", Stamp="5").text == "OK"
    assert Device.objects.get(serial_number=sim.serial).attphoto_stamp == "5"


def test_newer_protocol_endpoints(simulator):
    sim = simulator()
    assert sim.get("ping").text == "OK"
    reg = sim.post("registry", "DeviceType=acc,~DeviceName=X9,FWVersion=1.0")
    assert reg.text.startswith("RegistryCode=")
    again = sim.post("registry", "")
    assert again.text == reg.text  # stable code
    push = sim.post("push", "")
    assert "ATTLOGStamp=" in push.text and not push.text.startswith("GET OPTION")
    rt = sim.get("rtdata", type="rttime")
    assert rt.text.startswith("DateTime=") and "ServerTZ=+0100" in rt.text
    assert sim.get("rtdata", type="other").text == "OK"
    assert sim.post("querydata", "USER PIN=9\tName=Z", type="tabledata").status == 200
    assert sim.get("fdata").text == "OK"
    assert sim.get("cdata.aspx").status == 200


def test_device_time_param_records_drift(make_device, fpa):
    make_device("DRIFT1", timezone="UTC")
    from django.utils import timezone

    ahead = (timezone.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    Client().get("/iclock/getrequest", {"SN": "DRIFT1", "DeviceTime": ahead})
    device = Device.objects.get(serial_number="DRIFT1")
    assert 590 < device.clock_drift_seconds < 610


def test_online_transition_emits_signal(make_device):
    from fingerprint_attendance.signals import device_online

    make_device("ON1")
    received = []
    device_online.connect(lambda **kw: received.append(kw["device"].serial_number),
                          weak=False, dispatch_uid="t-online")
    try:
        client = Client()
        client.get("/iclock/ping", {"SN": "ON1"})
        client.get("/iclock/ping", {"SN": "ON1"})
    finally:
        device_online.disconnect(dispatch_uid="t-online")
    assert received == ["ON1"]


def test_operlog_oplog_events(simulator):
    from pathlib import Path

    sim = simulator()
    body = (Path(__file__).parent / "fixtures" / "adms" / "operlog_push2.txt").read_text()
    assert sim.post("cdata", body, table="OPERLOG", Stamp="3").status == 200
    assert DeviceEventLog.objects.filter(event_type=EventLogType.OPERLOG).count() == 2
    assert Device.objects.get(serial_number=sim.serial).operlog_stamp == "3"
    # template for unknown PIN 1001 was rejected and logged, never stored
    assert DeviceEventLog.objects.filter(event_type=EventLogType.TEMPLATE_REJECTED).exists()
    assert "TMP=" not in "".join(DeviceEventLog.objects.values_list("message", flat=True))


def test_custom_prefix(settings, make_device):
    from importlib import reload

    from django.urls import clear_url_caches

    import fingerprint_attendance.adms.urls as adms_urls
    import fingerprint_attendance.urls as root_urls
    import tests.urls as test_urls

    make_device("PFX1")
    settings.FINGERPRINT_ATTENDANCE = {**settings.FINGERPRINT_ATTENDANCE,
                                       "ADMS_URL_PREFIX": "secret-path/iclock/"}
    try:
        reload(adms_urls)
        reload(root_urls)
        reload(test_urls)
        clear_url_caches()
        assert Client().get("/secret-path/iclock/cdata", {"SN": "PFX1"}).status_code == 200
    finally:
        settings.FINGERPRINT_ATTENDANCE = {
            k: v for k, v in settings.FINGERPRINT_ATTENDANCE.items() if k != "ADMS_URL_PREFIX"}
        reload(adms_urls)
        reload(root_urls)
        reload(test_urls)
        clear_url_caches()
