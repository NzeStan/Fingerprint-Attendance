"""Registry of server -> device commands.

Each command type has a builder ``builder(adapter, device, payload) -> str`` producing the text
that follows ``C:<id>:`` in a ``getrequest`` response. Register your own::

    from fingerprint_attendance.adms.commands import command_registry, CommandSpec

    command_registry.register(CommandSpec(
        name="unlock_door",
        builder=lambda adapter, device, payload: "AC_UNLOCK",
        dangerous=True,
    ))

Protocol notes (firmware dependent, see docs/protocol.md):

* Push SDK 2.x firmware accepts ``DATA UPDATE USERINFO`` / ``DATA UPDATE FINGERTMP``.
* Firmware that uploads ``BIODATA`` (push 2.4+/3.x, ZKFinger v12 capable) expects
  ``DATA UPDATE BIODATA``.
* Very old iclock firmware uses ``DATA USER`` / ``DATA FP``; enable per device with
  ``options = {"legacy_commands": true}`` or subclass the adapter.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..utils.timeutils import device_zone, now, zk_encode_time

if TYPE_CHECKING:
    from ..models import Device
    from .adapter import ADMSProtocolAdapter

Builder = Callable[["ADMSProtocolAdapter", "Device", dict[str, Any]], str]


class CommandBuildError(Exception):
    """The command cannot be rendered (missing payload key, deleted template, ...)."""


@dataclass(frozen=True)
class CommandSpec:
    name: str
    builder: Builder
    description: str = ""
    #: payload keys that must be present
    required: tuple[str, ...] = ()
    #: carries biometric data: rendered only when handed to the device, stored redacted
    sensitive: bool = False
    #: destructive / disruptive: audited and permission-gated in the API
    dangerous: bool = False
    #: identifies the command family used for sync bookkeeping
    tags: frozenset[str] = field(default_factory=frozenset)


class CommandRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, CommandSpec] = {}

    def register(self, spec: CommandSpec, *, replace: bool = True) -> CommandSpec:
        if spec.name in self._specs and not replace:
            raise ValueError(f"command {spec.name!r} already registered")
        self._specs[spec.name] = spec
        return spec

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)

    def get(self, name: str) -> CommandSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise CommandBuildError(f"unknown command type {name!r}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return sorted(self._specs)

    def build(self, adapter: ADMSProtocolAdapter, device: Device, name: str,
              payload: dict[str, Any]) -> str:
        spec = self.get(name)
        missing = [k for k in spec.required if payload.get(k) in (None, "")]
        if missing:
            raise CommandBuildError(f"{name}: missing payload keys {missing}")
        text = spec.builder(adapter, device, payload)
        if "\n" in text or "\r" in text:
            raise CommandBuildError(f"{name}: rendered command contains a newline")
        return text


command_registry = CommandRegistry()


# --------------------------------------------------------------------------- helpers


def clean(value: Any) -> str:
    """Protocol fields are tab separated and line based: strip tabs/newlines."""
    return str("" if value is None else value).replace("\t", " ").replace("\r", " ").replace(
        "\n", " ")


def _legacy(device: Device) -> bool:
    return bool((device.options or {}).get("legacy_commands"))


def _template_b64(payload: dict[str, Any]) -> tuple[str, Any]:
    """Load the referenced template and return (base64 data, template)."""
    from ..models import FingerprintTemplate

    template_id = payload.get("template_id")
    try:
        template = FingerprintTemplate.objects.select_related("enrollee").get(pk=int(template_id or 0))
    except FingerprintTemplate.DoesNotExist:
        raise CommandBuildError(f"template {template_id} no longer exists") from None
    if payload.get("checksum") and payload["checksum"] != template.checksum:
        raise CommandBuildError("template changed since the command was queued (superseded)")
    return base64.b64encode(template.get_data()).decode("ascii"), template


# --------------------------------------------------------------------------- builders


def build_add_user(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
    fields = (
        f"PIN={clean(p['pin'])}\tName={clean(p.get('name', ''))[:24]}"
        f"\tPri={int(p.get('privilege', 0))}\tPasswd={clean(p.get('password', ''))}"
        f"\tCard={clean(p.get('card', ''))}\tGrp={int(p.get('group', 1))}"
        f"\tTZ={clean(p.get('timezones', '0000000100000000'))}\tVerify={int(p.get('verify', 0))}"
    )
    if _legacy(device):
        return f"DATA USER {fields}"
    return f"DATA UPDATE USERINFO {fields}"


def build_delete_user(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
    if _legacy(device):
        return f"DATA DEL_USER PIN={clean(p['pin'])}"
    return f"DATA DELETE USERINFO PIN={clean(p['pin'])}"


def build_add_template(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
    b64, template = _template_b64(p)
    pin = clean(p.get("pin") or template.enrollee.device_pin)
    finger = int(template.finger_index)
    if adapter.supports_biodata(device):
        major, _, minor = str(template.algorithm_version).partition(".")
        return (
            f"DATA UPDATE BIODATA Pin={pin}\tNo={finger}\tIndex=0\tValid=1"
            f"\tDuress={int(template.is_duress)}\tType=1\tMajorVer={major or 0}"
            f"\tMinorVer={minor or 0}\tFormat=0\tTmp={b64}"
        )
    prefix = "DATA FP" if _legacy(device) else "DATA UPDATE FINGERTMP"
    # ``Size`` is the length of the base64 text on the firmware we have fixtures for; some
    # documents describe it as the binary size. Override ``template_size_value`` if needed.
    size = adapter.template_size_value(b64, template)
    return f"{prefix} PIN={pin}\tFID={finger}\tSize={size}\tValid=1\tTMP={b64}"


def build_delete_template(adapter: ADMSProtocolAdapter, device: Device,
                          p: dict[str, Any]) -> str:
    pin, finger = clean(p["pin"]), int(p["finger_index"])
    if adapter.supports_biodata(device):
        return f"DATA DELETE BIODATA Pin={pin}\tType=1\tNo={finger}"
    if _legacy(device):
        return f"DATA DEL_FP PIN={pin}\tFID={finger}"
    return f"DATA DELETE FINGERTMP PIN={pin}\tFID={finger}"


def build_enroll(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
    from ..conf import settings

    pin, finger = clean(p["pin"]), int(p["finger_index"])
    retry = int(p.get("retry", settings.ENROLLMENT_RETRY_COUNT))
    overwrite = int(bool(p.get("overwrite", settings.ENROLLMENT_OVERWRITE)))
    if adapter.supports_biodata(device):
        return f"ENROLL_BIO TYPE=1\tPIN={pin}\tNO={finger}\tRETRY={retry}\tOVERWRITE={overwrite}"
    return f"ENROLL_FP PIN={pin}\tFID={finger}\tRETRY={retry}\tOVERWRITE={overwrite}"


def build_query_users(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
    if p.get("pin"):
        return f"DATA QUERY USERINFO PIN={clean(p['pin'])}"
    return "DATA QUERY USERINFO"


def build_query_templates(adapter: ADMSProtocolAdapter, device: Device,
                          p: dict[str, Any]) -> str:
    pin = clean(p["pin"])
    finger = p.get("finger_index")
    if adapter.supports_biodata(device):
        text = f"DATA QUERY BIODATA Type=1\tPIN={pin}"
        return text + (f"\tNo={int(finger)}" if finger is not None else "")
    text = f"DATA QUERY FINGERTMP PIN={pin}"
    return text + (f"\tFingerID={int(finger)}" if finger is not None else "")


def build_query_attlog(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
    return f"DATA QUERY ATTLOG StartTime={clean(p['start'])}\tEndTime={clean(p['end'])}"


def build_set_time(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
    """Set the device clock to server time (rendered when sent so the value is fresh)."""
    local = now().astimezone(device_zone(device)).replace(tzinfo=None)
    if p.get("datetime"):
        local = datetime.fromisoformat(p["datetime"]).replace(tzinfo=None)
    return adapter.render_set_time(device, local)


def build_set_option(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
    return f"SET OPTION {clean(p['key'])}={clean(p['value'])}"


def _static(text: str) -> Builder:
    def builder(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
        return text

    return builder


def build_raw(adapter: ADMSProtocolAdapter, device: Device, p: dict[str, Any]) -> str:
    return str(p["command"]).strip()


for _spec in (
    CommandSpec("add_user", build_add_user, "Create or update a user on the device",
                required=("pin",), tags=frozenset({"user", "sync"})),
    CommandSpec("delete_user", build_delete_user,
                "Delete a user (and their templates) from the device",
                required=("pin",), tags=frozenset({"user", "delete", "sync"})),
    CommandSpec("add_template", build_add_template, "Create or update a fingerprint template",
                required=("template_id",), sensitive=True, tags=frozenset({"template", "sync"})),
    CommandSpec("delete_template", build_delete_template, "Delete one fingerprint template",
                required=("pin", "finger_index"), tags=frozenset({"template", "delete", "sync"})),
    CommandSpec("enroll_fingerprint", build_enroll, "Start remote enrollment on the device",
                required=("pin", "finger_index"), tags=frozenset({"enroll"})),
    CommandSpec("query_users", build_query_users, "Ask the device to upload user records"),
    CommandSpec("query_templates", build_query_templates,
                "Ask the device to upload templates", required=("pin",)),
    CommandSpec("query_attlog", build_query_attlog,
                "Ask the device to re-upload punches in a time range",
                required=("start", "end")),
    CommandSpec("clear_logs", _static("CLEAR LOG"), "Delete all punches stored on the device",
                dangerous=True),
    CommandSpec("clear_data", _static("CLEAR DATA"),
                "Delete ALL data (users, templates, punches) on the device", dangerous=True),
    CommandSpec("set_time", build_set_time, "Set the device clock to server time"),
    CommandSpec("reboot", _static("REBOOT"), "Reboot the device", dangerous=True),
    CommandSpec("set_option", build_set_option, "Set a device option",
                required=("key", "value"), dangerous=True),
    CommandSpec("reload_options", _static("RELOAD OPTIONS"), "Reload device options"),
    CommandSpec("check", _static("CHECK"), "Ask the device to re-check the server / options"),
    CommandSpec("info", _static("INFO"), "Ask the device to report its information"),
    CommandSpec("upload_logs", _static("LOG"), "Ask the device to upload new logs now"),
    CommandSpec("raw", build_raw, "Send an arbitrary command string", required=("command",),
                dangerous=True),
):
    command_registry.register(_spec)


def encode_zk_datetime(local: datetime) -> int:
    return zk_encode_time(local)
