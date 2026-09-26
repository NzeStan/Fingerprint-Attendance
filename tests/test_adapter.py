"""Protocol adapter: fixture-based parsing per firmware variant and command rendering."""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path

import pytest

from fingerprint_attendance.adms.adapter import ADMSProtocolAdapter, get_adapter
from fingerprint_attendance.adms.commands import (
    CommandBuildError,
    CommandSpec,
    command_registry,
)
from fingerprint_attendance.constants import TemplateSource
from fingerprint_attendance.models import Device
from fingerprint_attendance.services import store_template
from fingerprint_attendance.utils.timeutils import zk_decode_time, zk_encode_time

FIXTURES = Path(__file__).parent / "fixtures" / "adms"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


adapter = ADMSProtocolAdapter()


@pytest.mark.parametrize(("name", "count"), [("attlog_legacy.txt", 3),
                                              ("attlog_push2.txt", 3),
                                              ("attlog_push3.txt", 2)])
def test_attlog_variants(name, count):
    result = adapter.parse_attlog(fixture(name))
    assert len(result.records) == count and not result.errors
    first = result.records[0]
    assert first.pin == "1001"
    assert first.local_time == datetime(2026, 1, 5, 8, 1, 2)


def test_attlog_fields_and_extras():
    records = adapter.parse_attlog(fixture("attlog_push3.txt")).records
    assert records[1].status == "4" and records[1].verify == 1 and records[1].work_code == "3"
    assert records[1].extra == ["0", "0", "1", "36.7", "0"]
    push2 = adapter.parse_attlog(fixture("attlog_push2.txt")).records
    assert push2[2].verify == 15 and push2[2].work_code == "7"


def test_attlog_tolerates_bad_lines():
    result = adapter.parse_attlog(fixture("attlog_malformed.txt"))
    assert [r.pin for r in result.records] == ["1001", "1003"]
    assert len(result.errors) == 3
    assert result.total == 5
    assert {e.line_no for e in result.errors} == {2, 3, 4}
    assert "error" in result.errors[0].as_dict()


def test_operlog_push2():
    batch = adapter.parse_operlog(fixture("operlog_push2.txt"))
    assert [o.op_type for o in batch.oplogs] == ["4", "6"]
    assert batch.oplogs[1].objects[0] == "1001"
    assert batch.users[0].pin == "1001" and batch.users[0].name == "Ada Lovelace"
    assert "Passwd" not in batch.users[0].data  # device passwords are never kept
    template = batch.templates[0]
    assert template.pin == "1001" and template.finger_index == 6 and template.kind == "fp"
    assert template.template == bytes(range(256)) * 3
    assert "TMP" not in template.data
    assert len(batch.errors) == 1  # FID=x
    assert batch.unknown == ["SOMETHING_NEW key=value"]
    assert batch.total == 6


def test_biodata_push3():
    batch = adapter.parse_biodata(fixture("biodata_push3.txt"))
    assert len(batch.templates) == 2  # the face (Type=9) is not a fingerprint
    first, second = batch.templates
    assert first.algorithm_version == "12" and first.finger_index == 5 and first.kind == \
        "biodata"
    assert second.pin == "1003" and second.duress and second.algorithm_version == "10"
    assert len(batch.unknown) == 1


def test_devicecmd_results():
    results = adapter.parse_devicecmd(fixture("devicecmd.txt"))
    assert [(r.command_id, r.return_code) for r in results] == [(11, 0), (12, -1), (13, 0)]
    info = results[2]
    assert info.data["DeviceName"] == "MB460" and info.data["TransactionCount"] == "1520"


def test_info_param_and_options():
    info = adapter.parse_info_param("Ver 6.60,25,40,1520,10.0.0.5,10,7,,0")
    assert info["user_count"] == "25" and info["fp_algorithm_version"] == "10"
    assert "face_enroll_count" not in info
    updates = adapter.device_updates_from_info(info)
    assert updates["reported_punch_count"] == 1520
    assert updates["fp_algorithm_version"] == "10"
    options = adapter.parse_options_body(fixture("options.txt"))
    updates = adapter.device_updates_from_info(options)
    assert updates["log_capacity"] == 200000
    assert updates["model_name"] == "SpeedFace-V5L"
    assert updates["platform"] == "ZMM220_TFT"
    assert updates["reported_template_count"] == 6
    assert adapter.parse_info_param(None) == {}


def test_kv_parsing_with_spaces():
    assert adapter.parse_kv("PIN=1 Name=John Smith Pri=0") == {"PIN": "1", "Name": "John Smith",
                                                                  "Pri": "0"}


def test_serial_and_params():
    assert adapter.get_serial({"sn": " ABC "}) == "ABC"
    assert adapter.get_serial({}) is None
    assert adapter.get_param({"PushVer": "2.4"}, "pushver") == "2.4"


def test_body_decoding(fpa):
    assert adapter.decode_body("Zoë".encode()) == "Zoë"
    assert adapter.decode_body("张三".encode("gbk")) == "张三"
    fpa(ADMS_BODY_ENCODINGS=["ascii"])
    assert "�" in adapter.decode_body(b"\xff\xfe")


def test_device_time_extraction():
    assert adapter.extract_device_time({"DeviceTime": "2026-01-05 08:00:00"}, {}) == \
        datetime(2026, 1, 5, 8)
    encoded = zk_encode_time(datetime(2026, 1, 5, 8, 30, 15))
    assert adapter.extract_device_time({"DevTime": str(encoded)}, {}) == \
        datetime(2026, 1, 5, 8, 30, 15)
    assert adapter.extract_device_time({}, {}) is None
    assert adapter.extract_device_time({"DeviceTime": "garbage"}, {}) is None
    assert adapter.extract_device_time({}, {"X-Device-Time": "2026-01-05 08:00:00"}) is not None


def test_zk_time_roundtrip():
    dt = datetime(2031, 12, 31, 23, 59, 58)
    assert zk_decode_time(zk_encode_time(dt)) == dt


@pytest.mark.django_db
def test_handshake_rendering(fpa, make_device):
    device = make_device("HS1", timezone="Africa/Lagos", attlog_stamp="99",
                         options={"Delay": 30, "legacy_commands": True})
    fpa(ADMS_HANDSHAKE_EXTRA_OPTIONS={"ServerName": "hr"})
    text = get_adapter(device).render_handshake(device)
    lines = text.splitlines()
    assert lines[0] == "GET OPTION FROM: HS1"
    options = dict(line.split("=", 1) for line in lines[1:])
    assert options["ATTLOGStamp"] == "99" and options["OPERLOGStamp"] == "None"
    assert options["TimeZone"] == "1"
    assert options["Delay"] == "30"  # per-device override
    assert options["ServerName"] == "hr"
    assert "legacy_commands" not in options
    fpa(ADMS_TIMEZONE_OPTION=3)
    assert "TimeZone=3" in get_adapter(device).render_handshake(device)


@pytest.mark.django_db
def test_half_hour_timezone(make_device):
    device = make_device(timezone="Asia/Kolkata")
    assert get_adapter(device).timezone_option(device) == 5.5


def test_render_commands():
    assert adapter.render_commands([]) == "OK"
    assert adapter.render_commands([(1, "REBOOT"), (2, "CHECK")]) == "C:1:REBOOT\nC:2:CHECK\n"
    assert adapter.render_ok(3) == "OK: 3"


@pytest.mark.django_db
class TestCommandBuilders:
    def build(self, device, name, payload):
        return get_adapter(device).build_command(device, name, payload)

    def test_user_commands(self, make_device):
        push2 = make_device(push_version="2.2.14")
        text = self.build(push2, "add_user", {"pin": "7", "name": "A\tB" + "x" * 40,
                                              "privilege": 14})
        assert text.startswith("DATA UPDATE USERINFO PIN=7\tName=A Bxxx")
        assert "\tPri=14\t" in text
        assert self.build(push2, "delete_user", {"pin": "7"}) == "DATA DELETE USERINFO PIN=7"
        legacy = make_device(options={"legacy_commands": True})
        assert self.build(legacy, "add_user", {"pin": "7"}).startswith("DATA USER PIN=7")
        assert self.build(legacy, "delete_user", {"pin": "7"}) == "DATA DEL_USER PIN=7"

    def test_template_commands_per_protocol(self, make_device, make_enrollee):
        enrollee = make_enrollee(pin="55")
        template, _ = store_template(enrollee, 3, "10", b"\x00\x01" * 50,
                                     source=TemplateSource.IMPORT, fan_out=False)
        b64 = base64.b64encode(b"\x00\x01" * 50).decode()
        payload = {"template_id": template.pk, "checksum": template.checksum}
        push2 = make_device(push_version="2.2.14")
        assert self.build(push2, "add_template", payload) == (
            f"DATA UPDATE FINGERTMP PIN=55\tFID=3\tSize={len(b64)}\tValid=1\tTMP={b64}")
        bio = make_device(push_version="2.4.1")
        text = self.build(bio, "add_template", payload)
        assert text.startswith("DATA UPDATE BIODATA Pin=55\tNo=3\tIndex=0\tValid=1")
        assert "MajorVer=10\tMinorVer=0" in text and text.endswith(f"Tmp={b64}")
        legacy = make_device(push_version="", options={"legacy_commands": True})
        assert self.build(legacy, "add_template", payload).startswith("DATA FP PIN=55")
        caps = make_device(push_version="", capabilities={"biodata": True})
        assert "BIODATA" in self.build(caps, "add_template", payload)
        assert self.build(bio, "delete_template", {"pin": "55", "finger_index": 3}) == \
            "DATA DELETE BIODATA Pin=55\tType=1\tNo=3"
        assert self.build(push2, "delete_template", {"pin": "55", "finger_index": 3}) == \
            "DATA DELETE FINGERTMP PIN=55\tFID=3"
        assert self.build(legacy, "delete_template", {"pin": "55", "finger_index": 3}) == \
            "DATA DEL_FP PIN=55\tFID=3"
        with pytest.raises(CommandBuildError, match="superseded"):
            self.build(push2, "add_template", {"template_id": template.pk, "checksum": "old"})
        with pytest.raises(CommandBuildError, match="no longer exists"):
            self.build(push2, "add_template", {"template_id": 999999})

    def test_other_commands(self, make_device, fpa):
        push2 = make_device(push_version="2.2.14", timezone="UTC")
        bio = make_device(push_version="3.1.2")
        assert self.build(push2, "enroll_fingerprint", {"pin": "5", "finger_index": 6}) == \
            "ENROLL_FP PIN=5\tFID=6\tRETRY=3\tOVERWRITE=1"
        assert self.build(bio, "enroll_fingerprint",
                          {"pin": "5", "finger_index": 6, "retry": 1, "overwrite": False}) == \
            "ENROLL_BIO TYPE=1\tPIN=5\tNO=6\tRETRY=1\tOVERWRITE=0"
        assert self.build(push2, "query_users", {}) == "DATA QUERY USERINFO"
        assert self.build(push2, "query_users", {"pin": "5"}) == "DATA QUERY USERINFO PIN=5"
        assert self.build(push2, "query_templates", {"pin": "5", "finger_index": 1}) == \
            "DATA QUERY FINGERTMP PIN=5\tFingerID=1"
        assert self.build(bio, "query_templates", {"pin": "5"}) == \
            "DATA QUERY BIODATA Type=1\tPIN=5"
        assert self.build(push2, "query_attlog", {"start": "a", "end": "b"}) == \
            "DATA QUERY ATTLOG StartTime=a\tEndTime=b"
        assert self.build(push2, "clear_logs", {}) == "CLEAR LOG"
        assert self.build(push2, "clear_data", {}) == "CLEAR DATA"
        assert self.build(push2, "reboot", {}) == "REBOOT"
        assert self.build(push2, "info", {}) == "INFO"
        assert self.build(push2, "check", {}) == "CHECK"
        assert self.build(push2, "reload_options", {}) == "RELOAD OPTIONS"
        assert self.build(push2, "upload_logs", {}) == "LOG"
        assert self.build(push2, "set_option", {"key": "Delay", "value": 5}) == \
            "SET OPTION Delay=5"
        assert self.build(push2, "raw", {"command": " SHELL ls "}) == "SHELL ls"
        encoded = self.build(push2, "set_time", {"datetime": "2026-01-05T08:00:00"})
        assert encoded == f"SET OPTION DateTime={zk_encode_time(datetime(2026, 1, 5, 8))}"
        push2.options = {"set_time_format": "iso"}
        assert self.build(push2, "set_time", {"datetime": "2026-01-05T08:00:00"}) == \
            "SET OPTION DateTime=2026-01-05 08:00:00"

    def test_registry_validation(self, make_device):
        device = make_device()
        with pytest.raises(CommandBuildError, match="missing"):
            self.build(device, "delete_user", {})
        with pytest.raises(CommandBuildError, match="unknown"):
            self.build(device, "nope", {})
        command_registry.register(CommandSpec("bad", lambda a, d, p: "A\nB"))
        try:
            with pytest.raises(CommandBuildError, match="newline"):
                self.build(device, "bad", {})
            with pytest.raises(ValueError):
                command_registry.register(CommandSpec("bad", lambda a, d, p: ""),
                                          replace=False)
        finally:
            command_registry.unregister("bad")
        assert "bad" not in command_registry


@pytest.mark.django_db
def test_per_device_adapter_override(make_device):
    device = make_device(options={"protocol_adapter": "tests.testapp.adapters.QuirkyAdapter"})
    from tests.testapp.adapters import QuirkyAdapter

    assert isinstance(get_adapter(device), QuirkyAdapter)
    assert isinstance(get_adapter(Device(serial_number="x")), ADMSProtocolAdapter)
