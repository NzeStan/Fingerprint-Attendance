"""ZKTeco ADMS / Push protocol adapter: parsing uploads and rendering responses.

The adapter is stateless apart from the device it is bound to. Subclass it for unusual
firmware and point ``ADMS_PROTOCOL_ADAPTER`` (or ``device.options["protocol_adapter"]``) at your
class. Parsing is tolerant by design:

* unknown keys are preserved in ``extra`` / the raw line,
* a malformed line becomes a :class:`ParseError`; the rest of the batch is still accepted,
* nothing here touches the database.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ..conf import import_object, settings
from ..utils.logging import redact
from ..utils.timeutils import device_zone, parse_device_datetime, utc_offset_hours, zk_encode_time
from .commands import command_registry

if TYPE_CHECKING:
    from ..models import Device

T = TypeVar("T")


# --------------------------------------------------------------------------- records


@dataclass
class ParseError:
    line_no: int
    line: str
    error: str

    def as_dict(self) -> dict[str, Any]:
        return {"line_no": self.line_no, "line": redact(self.line)[:500], "error": self.error}


@dataclass
class ParseResult(Generic[T]):
    records: list[T] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.records) + len(self.errors)


@dataclass
class AttLogRecord:
    pin: str
    local_time: datetime
    raw_time: str
    status: str = ""
    verify: int | None = None
    work_code: str = ""
    extra: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class UserRecord:
    pin: str
    name: str = ""
    privilege: int = 0
    data: dict[str, str] = field(default_factory=dict)


@dataclass
class TemplateRecord:
    pin: str
    finger_index: int
    template: bytes
    algorithm_version: str | None = None
    valid: bool = True
    duress: bool = False
    bio_type: int = 1
    kind: str = "fp"  # "fp" (legacy FP/FINGERTMP line) or "biodata"
    data: dict[str, str] = field(default_factory=dict)  # never contains the template


@dataclass
class OpLogRecord:
    op_type: str
    admin: str = ""
    local_time: datetime | None = None
    raw_time: str = ""
    objects: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class OperLogBatch:
    oplogs: list[OpLogRecord] = field(default_factory=list)
    users: list[UserRecord] = field(default_factory=list)
    templates: list[TemplateRecord] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (len(self.oplogs) + len(self.users) + len(self.templates) + len(self.unknown)
                + len(self.errors))


@dataclass
class CommandResult:
    command_id: int
    return_code: int
    cmd: str = ""
    data: dict[str, str] = field(default_factory=dict)
    body: str = ""


#: ``INFO`` query parameter of ``getrequest``, positional (firmware dependent tail).
INFO_FIELDS = (
    "firmware_version",
    "user_count",
    "fp_count",
    "attlog_count",
    "ip",
    "fp_algorithm_version",
    "face_algorithm_version",
    "face_enroll_count",
    "face_count",
    "dev_support",
)

#: device info keys (``table=options`` uploads and ``INFO`` command results) -> Device fields
DEVICE_INFO_KEYS: dict[str, tuple[str, str]] = {
    # key (lowercased, "~" stripped): (field, type)
    "devicename": ("model_name", "str"),
    "fwversion": ("firmware_version", "str"),
    "firmver": ("firmware_version", "str"),
    "firmware_version": ("firmware_version", "str"),
    "platform": ("platform", "str"),
    "fpversion": ("fp_algorithm_version", "str"),
    "zkfpversion": ("fp_algorithm_version", "str"),
    "fp_algorithm_version": ("fp_algorithm_version", "str"),
    "usercount": ("reported_user_count", "int"),
    "user_count": ("reported_user_count", "int"),
    "fpcount": ("reported_template_count", "int"),
    "fp_count": ("reported_template_count", "int"),
    "transactioncount": ("reported_punch_count", "int"),
    "attlogcount": ("reported_punch_count", "int"),
    "attlog_count": ("reported_punch_count", "int"),
    "facecount": ("reported_face_count", "int"),
    "face_count": ("reported_face_count", "int"),
    "maxattlogcount": ("log_capacity", "capacity"),
    "pushversion": ("push_version", "str"),
    "pushver": ("push_version", "str"),
}

_KV_SPLIT = re.compile(r"\s+(?=[A-Za-z~][\w~]*=)")


class ADMSProtocolAdapter:
    """Default adapter covering push protocol 2.x-3.x firmware (ATTLOG/OPERLOG/BIODATA)."""

    #: BIODATA is used when the device reports at least this push version
    biodata_min_push_version = (2, 4, 0)

    def __init__(self, device: Device | None = None) -> None:
        self.device = device

    # ------------------------------------------------------------------ requests
    @staticmethod
    def get_param(params: Mapping[str, Any], *names: str) -> str | None:
        lowered = {k.lower(): v for k, v in params.items()}
        for name in names:
            value = lowered.get(name.lower())
            if value not in (None, ""):
                return str(value)
        return None

    def get_serial(self, params: Mapping[str, Any]) -> str | None:
        serial = self.get_param(params, "SN", "sn", "serial")
        return serial.strip() if serial else None

    def decode_body(self, body: bytes) -> str:
        for encoding in settings.ADMS_BODY_ENCODINGS or ["utf-8"]:
            try:
                return body.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return body.decode("utf-8", errors="replace")

    @staticmethod
    def iter_lines(text: str) -> Iterator[tuple[int, str]]:
        for i, line in enumerate(text.splitlines(), start=1):
            line = line.strip("\r\n\x00")
            if line.strip():
                yield i, line

    def extract_device_time(self, params: Mapping[str, Any],
                            headers: Mapping[str, Any]) -> datetime | None:
        """Device clock, when the firmware sends it (naive local time).

        No standard parameter exists across firmware; ``DeviceTime``/``DevTime`` query params
        and an ``X-Device-Time`` header are recognised. Pull mode reads the clock directly.
        """
        value = self.get_param(params, "DeviceTime", "DevTime", "devicetime")
        if not value:
            value = headers.get("X-Device-Time") or headers.get("HTTP_X_DEVICE_TIME")
        if not value:
            return None
        text = str(value).strip()
        from ..utils.timeutils import zk_decode_time

        try:
            if text.isdigit() and len(text) != 14:  # ZK compact encoding, not YYYYMMDDhhmmss
                return zk_decode_time(int(text))
            return parse_device_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None

    # ------------------------------------------------------------------ protocol version
    @staticmethod
    def parse_version(value: str | None) -> tuple[int, ...]:
        if not value:
            return ()
        nums = re.findall(r"\d+", value)
        return tuple(int(n) for n in nums[:3])

    def supports_biodata(self, device: Device) -> bool:
        caps = device.capabilities or {}
        if "biodata" in caps:
            return bool(caps["biodata"])
        opts = device.options or {}
        if "biodata" in opts:
            return bool(opts["biodata"])
        version = self.parse_version(device.push_version)
        return bool(version) and version >= self.biodata_min_push_version

    # ------------------------------------------------------------------ key/value parsing
    def parse_kv(self, text: str) -> dict[str, str]:
        """``PIN=1\\tName=John`` -> dict. Tabs are the separator; firmware that uses spaces is
        handled when every token looks like ``key=value``."""
        parts = text.split("\t") if "\t" in text else _KV_SPLIT.split(text.strip())
        out: dict[str, str] = {}
        for part in parts:
            if "=" not in part:
                continue
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
        return out

    # ------------------------------------------------------------------ ATTLOG
    def parse_attlog(self, text: str) -> ParseResult[AttLogRecord]:
        result: ParseResult[AttLogRecord] = ParseResult()
        for line_no, line in self.iter_lines(text):
            try:
                result.records.append(self.parse_attlog_line(line))
            except (ValueError, IndexError) as exc:
                result.errors.append(ParseError(line_no, line, str(exc)))
        return result

    def parse_attlog_line(self, line: str) -> AttLogRecord:
        if "\t" in line:
            fields = line.split("\t")
        else:  # "PIN YYYY-MM-DD HH:MM:SS status verify ..." (space separated firmware)
            tokens = line.split()
            if len(tokens) < 3:
                raise ValueError("too few fields")
            fields = [tokens[0], f"{tokens[1]} {tokens[2]}", *tokens[3:]]
        if len(fields) < 2:
            raise ValueError("too few fields")
        pin = fields[0].strip()
        if not pin:
            raise ValueError("empty PIN")
        raw_time = fields[1].strip()
        local_time = parse_device_datetime(raw_time)
        status = fields[2].strip() if len(fields) > 2 else ""
        verify = _int_or_none(fields[3]) if len(fields) > 3 else None
        work_code = fields[4].strip() if len(fields) > 4 else ""
        return AttLogRecord(pin=pin, local_time=local_time, raw_time=raw_time, status=status,
                            verify=verify, work_code=work_code,
                            extra=[f.strip() for f in fields[5:]], raw=line)

    # ------------------------------------------------------------------ OPERLOG / BIODATA
    def parse_operlog(self, text: str) -> OperLogBatch:
        batch = OperLogBatch()
        for line_no, line in self.iter_lines(text):
            head, _, rest = line.partition(" ")
            kind = head.strip().upper()
            try:
                if kind == "OPLOG":
                    batch.oplogs.append(self.parse_oplog_line(rest))
                elif kind in ("USER", "USERINFO"):
                    batch.users.append(self.parse_user_line(rest))
                elif kind in ("FP", "FINGERTMP"):
                    batch.templates.append(self.parse_fp_line(rest))
                elif kind == "BIODATA":
                    record = self.parse_biodata_line(rest)
                    if record is not None:
                        batch.templates.append(record)
                    else:
                        batch.unknown.append(redact(line))
                else:
                    batch.unknown.append(redact(line))
            except (ValueError, KeyError, IndexError, binascii.Error) as exc:
                batch.errors.append(ParseError(line_no, line, str(exc)))
        return batch

    def parse_biodata(self, text: str) -> OperLogBatch:
        """``table=BIODATA`` uploads: lines may or may not start with ``BIODATA``."""
        normalised = "\n".join(
            line if line.upper().startswith(("BIODATA ", "FP ", "USER ", "OPLOG ")) else
            f"BIODATA {line}" for _, line in self.iter_lines(text)
        )
        return self.parse_operlog(normalised)

    def parse_oplog_line(self, rest: str) -> OpLogRecord:
        fields = rest.split("\t")
        op_type = fields[0].strip()
        if not op_type:
            raise ValueError("empty OPLOG type")
        admin = fields[1].strip() if len(fields) > 1 else ""
        raw_time = fields[2].strip() if len(fields) > 2 else ""
        local_time = parse_device_datetime(raw_time) if raw_time else None
        return OpLogRecord(op_type=op_type, admin=admin, local_time=local_time,
                           raw_time=raw_time, objects=[f.strip() for f in fields[3:]],
                           raw=f"OPLOG {rest}")

    def parse_user_line(self, rest: str) -> UserRecord:
        data = self.parse_kv(rest)
        pin = data.get("PIN") or data.get("Pin") or data.get("pin")
        if not pin:
            raise ValueError("USER line without PIN")
        data.pop("Passwd", None)  # never keep device passwords
        return UserRecord(pin=pin, name=data.get("Name", ""),
                          privilege=_int_or_none(data.get("Pri")) or 0, data=data)

    def _decode_template(self, value: str | None) -> bytes:
        if not value:
            raise ValueError("template data missing")
        text = value.strip()
        text += "=" * (-len(text) % 4)
        return base64.b64decode(text, validate=False)

    def parse_fp_line(self, rest: str) -> TemplateRecord:
        data = self.parse_kv(rest)
        pin = data.get("PIN") or data.get("Pin")
        if not pin:
            raise ValueError("FP line without PIN")
        fid = _int_or_none(data.get("FID") if "FID" in data else data.get("FingerID"))
        if fid is None:
            raise ValueError("FP line without FID")
        template = self._decode_template(data.pop("TMP", None) or data.pop("Tmp", None))
        return TemplateRecord(pin=pin, finger_index=fid, template=template,
                              algorithm_version=None,
                              valid=data.get("Valid", "1") != "0",
                              duress=data.get("Valid") == "3", kind="fp", data=data)

    def parse_biodata_line(self, rest: str) -> TemplateRecord | None:
        """Return ``None`` for non-fingerprint biometrics (face, palm, ...)."""
        data = self.parse_kv(rest)
        lowered = {k.lower(): v for k, v in data.items()}
        bio_type = _int_or_none(lowered.get("type"))
        if bio_type is None:
            bio_type = 1
        if bio_type != 1:
            return None
        pin = lowered.get("pin")
        if not pin:
            raise ValueError("BIODATA line without Pin")
        finger = _int_or_none(lowered.get("no"))
        if finger is None:
            raise ValueError("BIODATA line without No")
        template = self._decode_template(lowered.get("tmp"))
        for key in [k for k in data if k.lower() == "tmp"]:
            data.pop(key)
        major = lowered.get("majorver") or ""
        minor = lowered.get("minorver") or ""
        algorithm = major if not minor or minor == "0" else f"{major}.{minor}"
        return TemplateRecord(pin=pin, finger_index=finger, template=template,
                              algorithm_version=algorithm or None,
                              valid=lowered.get("valid", "1") != "0",
                              duress=lowered.get("duress", "0") == "1", bio_type=bio_type,
                              kind="biodata", data=data)

    # ------------------------------------------------------------------ devicecmd
    def parse_devicecmd(self, text: str) -> list[CommandResult]:
        results: list[CommandResult] = []
        current: CommandResult | None = None
        for _, line in self.iter_lines(text):
            if line.upper().startswith("ID="):
                pairs = dict(p.partition("=")[::2] for p in line.split("&") if "=" in p)
                pairs = {k.strip(): v.strip() for k, v in pairs.items()}
                try:
                    current = CommandResult(
                        command_id=int(pairs.get("ID", "")),
                        return_code=int(pairs.get("Return", pairs.get("return", "0")) or 0),
                        cmd=pairs.get("CMD", ""),
                    )
                except ValueError:
                    current = None
                    continue
                results.append(current)
            elif current is not None:
                key, sep, value = line.lstrip("~").partition("=")
                if sep:
                    current.data[key.strip()] = value.strip()
                current.body = (current.body + "\n" + line).strip()
        return results

    # ------------------------------------------------------------------ device info
    def parse_info_param(self, info: str | None) -> dict[str, str]:
        if not info:
            return {}
        values = [v.strip() for v in info.split(",")]
        return {name: values[i] for i, name in enumerate(INFO_FIELDS) if i < len(values)
                and values[i] != ""}

    def parse_options_body(self, text: str) -> dict[str, str]:
        """``table=options`` upload: ``~DeviceName=X,FWVersion=Y,...`` (comma or newline)."""
        out: dict[str, str] = {}
        for chunk in re.split(r"[,\n\r\t]+", text):
            key, sep, value = chunk.strip().partition("=")
            if sep and key:
                out[key.strip()] = value.strip()
        return out

    def device_updates_from_info(self, info: Mapping[str, str]) -> dict[str, Any]:
        """Map reported device information to Device field updates."""
        updates: dict[str, Any] = {}
        for key, value in info.items():
            mapped = DEVICE_INFO_KEYS.get(key.lstrip("~").lower())
            if not mapped or value in (None, ""):
                continue
            field_name, kind = mapped
            if kind == "int":
                number = _int_or_none(value)
                if number is not None:
                    updates[field_name] = number
            elif kind == "capacity":
                number = _int_or_none(value)
                if number:
                    # Firmware reports MaxAttLogCount in units of 10,000 records.
                    updates[field_name] = number * 10000 if number < 1000 else number
            else:
                updates[field_name] = str(value)[:100]
        return updates

    # ------------------------------------------------------------------ rendering
    def timezone_option(self, device: Device) -> int | float:
        if settings.ADMS_TIMEZONE_OPTION is not None:
            return int(settings.ADMS_TIMEZONE_OPTION)
        hours = utc_offset_hours(device_zone(device))
        return int(hours) if float(hours).is_integer() else hours

    def handshake_options(self, device: Device) -> dict[str, Any]:
        def stamp(value: str) -> str:
            return value if value else "None"

        options: dict[str, Any] = {
            "ATTLOGStamp": stamp(device.attlog_stamp),
            "OPERLOGStamp": stamp(device.operlog_stamp),
            "ATTPHOTOStamp": stamp(device.attphoto_stamp),
            "BIODATAStamp": stamp(device.biodata_stamp),
            "ErrorDelay": settings.ADMS_ERROR_DELAY,
            "Delay": settings.ADMS_DELAY,
            "TransTimes": settings.ADMS_TRANS_TIMES,
            "TransInterval": settings.ADMS_TRANS_INTERVAL,
            "TransFlag": settings.ADMS_TRANS_FLAG,
            "TimeZone": self.timezone_option(device),
            "Realtime": int(bool(settings.ADMS_REALTIME)),
            "Encrypt": settings.ADMS_ENCRYPT,
            "ServerVer": settings.ADMS_SERVER_VERSION,
            "PushProtVer": settings.ADMS_PUSH_PROTOCOL_VERSION,
        }
        options.update(settings.ADMS_HANDSHAKE_EXTRA_OPTIONS or {})
        reserved = {"protocol_adapter", "legacy_commands", "biodata", "set_time_format"}
        options.update({k: v for k, v in (device.options or {}).items() if k not in reserved})
        return options

    def render_handshake(self, device: Device) -> str:
        lines = [f"GET OPTION FROM: {device.serial_number}"]
        lines.extend(f"{key}={value}" for key, value in self.handshake_options(device).items())
        return "\n".join(lines) + "\n"

    def render_commands(self, commands: Iterable[tuple[int, str]]) -> str:
        rendered = [f"C:{cid}:{text}" for cid, text in commands]
        return ("\n".join(rendered) + "\n") if rendered else "OK"

    def render_ok(self, count: int | None = None) -> str:
        return "OK" if count is None else f"OK: {count}"

    def render_registry(self, device: Device, code: str) -> str:
        return f"RegistryCode={code}"

    def render_rttime(self, device: Device) -> str:
        from ..utils.timeutils import format_offset, now

        zone = device_zone(device)
        local = now().astimezone(zone).replace(tzinfo=None)
        return f"DateTime={zk_encode_time(local)},ServerTZ={format_offset(zone)}"

    def render_set_time(self, device: Device, local: datetime) -> str:
        """Firmware differs: default is ``SET OPTION DateTime=<zk-encoded>``; set
        ``device.options["set_time_format"] = "iso"`` for ``YYYY-MM-DD HH:MM:SS``."""
        fmt = (device.options or {}).get("set_time_format", "zk")
        if fmt == "iso":
            return f"SET OPTION DateTime={local:%Y-%m-%d %H:%M:%S}"
        return f"SET OPTION DateTime={zk_encode_time(local)}"

    def template_size_value(self, b64: str, template: Any) -> int:
        return len(b64)

    def build_command(self, device: Device, command_type: str, payload: dict[str, Any]) -> str:
        return command_registry.build(self, device, command_type, payload)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def get_adapter(device: Device | None = None) -> ADMSProtocolAdapter:
    path = None
    if device is not None:
        path = (device.options or {}).get("protocol_adapter")
    cls = import_object(path, setting="protocol_adapter") if path else settings.import_(
        "ADMS_PROTOCOL_ADAPTER")
    return cls(device)
