"""Configuration for django-fingerprint-attendance.

Every setting resolves in this order:

1. the Django settings dict ``FINGERPRINT_ATTENDANCE = {...}``
2. an environment variable prefixed ``FPA_`` (e.g. ``FPA_ADMS_URL_PREFIX``), cast to the
   setting's declared type
3. the package default declared in :data:`SETTINGS_SPEC`

Use the module-level :data:`settings` object::

    from fingerprint_attendance.conf import settings
    settings.ADMS_URL_PREFIX
    settings.import_("PIN_GENERATOR")   # resolve a dotted-path setting to the object

The object is lazy, cached, and reset whenever Django's ``setting_changed`` signal fires
for ``FINGERPRINT_ATTENDANCE`` (so ``override_settings`` works in tests).
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.utils.module_loading import import_string

#: Name of the Django settings dict. Change here to rename it everywhere.
SETTINGS_NAME = "FINGERPRINT_ATTENDANCE"
#: Prefix for environment variables.
ENV_PREFIX = "FPA_"

_MISSING = object()


# --------------------------------------------------------------------------- types / casting


class SettingType:
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    LIST = "list"  # list[str]
    INT_LIST = "int_list"
    DICT = "dict"  # JSON object
    JSON = "json"  # any JSON value
    DURATION = "duration"  # timedelta
    TIME = "time"  # datetime.time
    PATH = "path"  # dotted import path (or the object itself)
    PATH_LIST = "path_list"


_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}
_NULL = {"", "none", "null"}
_DURATION_RE = re.compile(r"^\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h|d|w)?\s*$", re.I)
_DURATION_UNITS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    None: 1,
}


def parse_duration_value(value: Any) -> timedelta:
    """Convert ``timedelta``/number-of-seconds/``"15m"``/``"HH:MM:SS"`` to ``timedelta``."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ValueError("booleans are not durations")
    if isinstance(value, (int, float)):
        return timedelta(seconds=value)
    if isinstance(value, str):
        m = _DURATION_RE.match(value)
        if m:
            unit = m.group("unit")
            return timedelta(
                seconds=float(m.group("num")) * _DURATION_UNITS[unit.lower() if unit else None]
            )
        parts = value.strip().split(":")
        if len(parts) in (2, 3) and all(p.strip().isdigit() for p in parts):
            nums = [int(p) for p in parts]
            if len(nums) == 2:
                nums.append(0)
            return timedelta(hours=nums[0], minutes=nums[1], seconds=nums[2])
        from django.utils.dateparse import parse_duration

        parsed = parse_duration(value)
        if parsed is not None:
            return parsed
    raise ValueError(f"cannot interpret {value!r} as a duration")


def parse_time_value(value: Any) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        parts = value.strip().split(":")
        if 2 <= len(parts) <= 3:
            nums = [int(p) for p in parts]
            return time(nums[0], nums[1], nums[2] if len(nums) > 2 else 0)
    raise ValueError(f"cannot interpret {value!r} as a time (expected HH:MM)")


def _cast_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"cannot interpret {value!r} as a boolean")


def _cast_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("["):
            loaded = json.loads(s)
            if not isinstance(loaded, list):
                raise ValueError("expected a JSON array")
            return loaded
        return [item.strip() for item in s.split(",") if item.strip()]
    raise ValueError(f"cannot interpret {value!r} as a list")


def _cast_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        loaded = json.loads(value)
        if not isinstance(loaded, dict):
            raise ValueError("expected a JSON object")
        return loaded
    raise ValueError(f"cannot interpret {value!r} as a mapping")


def cast_value(value: Any, type_: str, *, from_env: bool = False) -> Any:
    """Cast a raw value (from settings or env) to ``type_``."""
    if type_ == SettingType.BOOL:
        return _cast_bool(value)
    if type_ == SettingType.INT:
        if isinstance(value, bool):
            raise ValueError("booleans are not integers")
        return int(value)
    if type_ == SettingType.FLOAT:
        return float(value)
    if type_ == SettingType.STR:
        return str(value)
    if type_ == SettingType.LIST:
        return [str(v) for v in _cast_list(value)]
    if type_ == SettingType.INT_LIST:
        return [int(v) for v in _cast_list(value)]
    if type_ == SettingType.PATH_LIST:
        return [v if not isinstance(v, str) else v.strip() for v in _cast_list(value)]
    if type_ == SettingType.DICT:
        return _cast_dict(value)
    if type_ == SettingType.JSON:
        if from_env and isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value
    if type_ == SettingType.DURATION:
        return parse_duration_value(value)
    if type_ == SettingType.TIME:
        return parse_time_value(value)
    if type_ == SettingType.PATH:
        return value.strip() if isinstance(value, str) else value
    raise ValueError(f"unknown setting type {type_!r}")  # pragma: no cover


# --------------------------------------------------------------------------- spec


@dataclass(frozen=True)
class Setting:
    name: str
    default: Any
    type: str
    description: str
    group: str
    nullable: bool = False
    #: short names accepted in place of a dotted path (``"celery"`` -> dotted path)
    aliases: Mapping[str, str] = field(default_factory=dict)
    #: callable computing the default lazily (e.g. from other Django settings)
    default_factory: Callable[[], Any] | None = None

    @property
    def env_var(self) -> str:
        return f"{ENV_PREFIX}{self.name}"

    @property
    def is_import(self) -> bool:
        return self.type in (SettingType.PATH, SettingType.PATH_LIST)

    def get_default(self) -> Any:
        if self.default_factory is not None:
            return self.default_factory()
        # Defensive copy so callers can never mutate the spec.
        return copy.deepcopy(self.default)

    def display_default(self) -> str:
        if self.default_factory is not None:
            return (self.default_factory.__doc__ or "computed").strip().strip("`")
        value = self.default
        if isinstance(value, timedelta):
            return _format_duration(value)
        if isinstance(value, time):
            return value.strftime("%H:%M")
        return repr(value)


def _format_duration(td: timedelta) -> str:
    seconds = int(td.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _default_employee_model() -> str:
    """``settings.AUTH_USER_MODEL``"""
    from django.conf import settings as dj

    return str(dj.AUTH_USER_MODEL)


def _default_timezone() -> str:
    """``settings.TIME_ZONE``"""
    from django.conf import settings as dj

    return str(getattr(dj, "TIME_ZONE", "UTC") or "UTC")


P = "fingerprint_attendance"
T = SettingType
_S = Setting

SETTINGS_SPEC: tuple[Setting, ...] = (
    # ------------------------------------------------------------------ identity
    _S("EMPLOYEE_MODEL", None, T.STR,
       "Model enrollees link to (``app_label.Model``). Set before the first migrate, like "
       "``AUTH_USER_MODEL``.", "Identity", default_factory=_default_employee_model),
    _S("EMPLOYEE_DISPLAY_FIELD", None, T.STR,
       "Employee attribute used as the name sent to devices (``__`` traverses relations), or a "
       "dotted path to ``callable(employee) -> str``. ``None`` uses ``get_full_name()``/``str()``.",
       "Identity", nullable=True),
    _S("EMPLOYEE_LOOKUP_FIELD", "pk", T.STR,
       "Employee field the API accepts to identify an employee when creating enrollees.",
       "Identity"),
    # ------------------------------------------------------------------ pins
    _S("PIN_GENERATOR", "sequential", T.PATH,
       "``callable(employee) -> str`` producing a device PIN. Aliases: ``sequential``, "
       "``employee_field``.", "Device PIN",
       aliases={"sequential": f"{P}.pins.sequential_pin",
                "employee_field": f"{P}.pins.employee_field_pin"}),
    _S("PIN_SOURCE_FIELD", None, T.STR,
       "Employee field used by the ``employee_field`` PIN generator (defaults to "
       "``EMPLOYEE_LOOKUP_FIELD``).", "Device PIN", nullable=True),
    _S("PIN_START", 1, T.INT, "First PIN issued by the sequential generator.", "Device PIN"),
    _S("PIN_MAX_LENGTH", 9, T.INT,
       "Maximum PIN length accepted by your devices (many ZKTeco models: 9 digits).",
       "Device PIN"),
    _S("PIN_NUMERIC_ONLY", True, T.BOOL, "Reject PINs that are not all digits.", "Device PIN"),
    _S("PIN_REUSE_ALLOWED", False, T.BOOL,
       "Allow a PIN of a deleted enrollee to be issued again. When false, retired PINs are "
       "remembered so old punches are never attributed to a new person.", "Device PIN"),
    # ------------------------------------------------------------------ ADMS
    _S("ADMS_ENABLED", True, T.BOOL, "Serve the ADMS/Push endpoints.", "ADMS"),
    _S("ADMS_URL_PREFIX", "iclock/", T.STR,
       "URL prefix the devices call (set the device's server path accordingly).", "ADMS"),
    _S("ADMS_AUTO_REGISTER_DEVICES", True, T.BOOL,
       "Create a Device row when an unknown serial number connects.", "ADMS"),
    _S("ADMS_REQUIRE_DEVICE_APPROVAL", True, T.BOOL,
       "Auto-registered devices start as ``pending_approval``; their data is refused (and kept "
       "on the device) until approved.", "ADMS"),
    _S("ADMS_ALLOWED_IPS", [], T.LIST,
       "IP addresses / CIDR networks allowed to call ADMS endpoints (empty = any).", "ADMS"),
    _S("ADMS_TRUSTED_PROXY_IPS", [], T.LIST,
       "Reverse proxies whose ``X-Forwarded-For`` header is trusted for the device IP.", "ADMS"),
    _S("ADMS_DEVICE_TOKEN_REQUIRED", False, T.BOOL,
       "Require a per-device token (query param, ``X-FPA-Device-Token`` header or "
       "``pushcommkey``).", "ADMS"),
    _S("ADMS_TOKEN_PARAM", "token", T.STR, "Query parameter carrying the device token.", "ADMS"),
    _S("ADMS_ERROR_DELAY", 30, T.INT, "Handshake ``ErrorDelay`` (seconds between retries).",
       "ADMS"),
    _S("ADMS_DELAY", 10, T.INT, "Handshake ``Delay`` (seconds between ``getrequest`` polls).",
       "ADMS"),
    _S("ADMS_TRANS_TIMES", "00:00;14:05", T.STR, "Handshake ``TransTimes``.", "ADMS"),
    _S("ADMS_TRANS_INTERVAL", 1, T.INT, "Handshake ``TransInterval`` (minutes).", "ADMS"),
    _S("ADMS_TRANS_FLAG", "TransData AttLog\tOpLog\tEnrollUser\tChgUser\tEnrollFP\tChgFP",
       T.STR, "Handshake ``TransFlag`` (which data the device uploads).", "ADMS"),
    _S("ADMS_REALTIME", True, T.BOOL, "Handshake ``Realtime`` (upload punches immediately).",
       "ADMS"),
    _S("ADMS_ENCRYPT", "None", T.STR, "Handshake ``Encrypt`` value.", "ADMS"),
    _S("ADMS_TIMEZONE_OPTION", None, T.INT,
       "Force the handshake ``TimeZone`` value; ``None`` computes it from the device timezone.",
       "ADMS", nullable=True),
    _S("ADMS_SERVER_VERSION", "2.4.1", T.STR, "``ServerVer`` reported in the handshake.", "ADMS"),
    _S("ADMS_PUSH_PROTOCOL_VERSION", "2.4.1", T.STR,
       "``PushProtVer`` reported in the handshake.", "ADMS"),
    _S("ADMS_HANDSHAKE_EXTRA_OPTIONS", {}, T.DICT,
       "Extra ``key=value`` lines appended to every handshake (device ``options`` override "
       "these).", "ADMS"),
    _S("ADMS_MAX_COMMANDS_PER_REQUEST", 20, T.INT,
       "Maximum commands handed out per ``getrequest``.", "ADMS"),
    _S("ADMS_PROTOCOL_ADAPTER", f"{P}.adms.adapter.ADMSProtocolAdapter", T.PATH,
       "Class parsing/rendering the protocol. Subclass for unusual firmware (a device can also "
       "name one in ``options['protocol_adapter']``).", "ADMS"),
    _S("ADMS_MAX_REQUEST_BYTES", 20 * 1024 * 1024, T.INT,
       "Maximum ADMS request body size (larger requests get HTTP 413).", "ADMS"),
    _S("ADMS_RATE_LIMIT", "600/m", T.STR,
       "Per-device request rate limit (``N/s|m|h``) using the Django cache. ``None`` disables.",
       "ADMS", nullable=True),
    _S("ADMS_BODY_ENCODINGS", ["utf-8", "gbk", "latin-1"], T.LIST,
       "Encodings tried, in order, when decoding device uploads.", "ADMS"),
    _S("ADMS_ACCEPT_FROM_PENDING_DEVICES", False, T.BOOL,
       "Store punches from devices awaiting approval (flagged ``pending_device``) instead of "
       "refusing them.", "ADMS"),
    _S("ADMS_STORE_OPERLOG_EVENTS", True, T.BOOL,
       "Store OPLOG lines as DeviceEventLog rows.", "ADMS"),
    # ------------------------------------------------------------------ pull
    _S("PULL_ENABLED", False, T.BOOL, "Enable pull mode (requires the ``[pull]`` extra).", "Pull"),
    _S("PULL_ADAPTER", f"{P}.pull.pyzk_adapter.PyZKPullAdapter", T.PATH,
       "``BasePullAdapter`` subclass used to talk to devices on the LAN.", "Pull"),
    _S("PULL_DEFAULT_PORT", 4370, T.INT, "Default device TCP/UDP port.", "Pull"),
    _S("PULL_TIMEOUT", 10, T.INT, "Connection timeout in seconds.", "Pull"),
    _S("PULL_COMM_KEY", 0, T.INT, "Default device comm key (password).", "Pull"),
    _S("PULL_FORCE_UDP", False, T.BOOL, "Force UDP (older devices).", "Pull"),
    _S("PULL_POLL_INTERVAL", timedelta(seconds=60), T.DURATION,
       "Interval between polls for ``fpa_pull_attendance --loop``.", "Pull"),
    _S("PULL_RECONNECT_BACKOFF_MAX", timedelta(minutes=5), T.DURATION,
       "Maximum reconnect backoff for live capture.", "Pull"),
    _S("PULL_DISABLE_DEVICE_DURING_SYNC", True, T.BOOL,
       "Lock the device keypad while bulk reading/writing.", "Pull"),
    # ------------------------------------------------------------------ enrollment
    _S("FINGERS_REQUIRED_MIN", 1, T.INT,
       "Minimum enrolled fingers an enrollee must have when a session completes.", "Enrollment"),
    _S("FINGERS_ALLOWED_MAX", 10, T.INT, "Maximum fingers stored per enrollee.", "Enrollment"),
    _S("ALLOWED_FINGER_INDEXES", list(range(10)), T.INT_LIST,
       "Finger indexes (0-9, ZKTeco numbering) that may be enrolled.", "Enrollment"),
    _S("ENROLLMENT_SESSION_TTL", timedelta(minutes=10), T.DURATION,
       "Lifetime of an enrollment session.", "Enrollment"),
    _S("ENROLLMENT_REQUIRE_CONSENT", True, T.BOOL,
       "Refuse to store templates for enrollees without active consent.", "Enrollment"),
    _S("ENROLLMENT_RETRY_COUNT", 3, T.INT, "``RETRY`` parameter of remote enroll commands.",
       "Enrollment"),
    _S("ENROLLMENT_OVERWRITE", True, T.BOOL, "``OVERWRITE`` parameter of remote enroll commands.",
       "Enrollment"),
    _S("ACCEPT_DEVICE_ENROLLMENTS", True, T.BOOL,
       "Accept templates enrolled at the device (walk-up) and fan them out.", "Enrollment"),
    _S("DELETE_REJECTED_DEVICE_TEMPLATES", True, T.BOOL,
       "When a walk-up template is refused (no consent, finger not allowed, unknown PIN), queue "
       "its deletion on that device.", "Enrollment"),
    _S("AGENT_CAN_START_SESSIONS", False, T.BOOL,
       "Allow desktop agents to open enrollment sessions themselves.", "Enrollment"),
    _S("AGENT_AUTH_SCHEME", "Agent", T.STR,
       "``Authorization: <scheme> <key>`` scheme for agent requests.", "Enrollment"),
    _S("AGENT_RATE_LIMIT", "120/m", T.STR, "Per-agent request rate limit.", "Enrollment",
       nullable=True),
    # ------------------------------------------------------------------ templates
    _S("TEMPLATE_ENCRYPTION_ENABLED", True, T.BOOL, "Encrypt templates at rest.", "Templates"),
    _S("TEMPLATE_ENCRYPTION_KEYS", [], T.LIST,
       "Fernet keys, newest first. Old keys still decrypt (rotation); run "
       "``fpa_rotate_template_keys`` after adding a key.", "Templates"),
    _S("TEMPLATE_STORAGE_BACKEND", f"{P}.crypto.DatabaseTemplateStorage", T.PATH,
       "Class storing template bytes (e.g. to use an external vault).", "Templates"),
    _S("EXPOSE_TEMPLATE_DATA_IN_API", False, T.BOOL,
       "Allow the API to return template bytes to callers holding the "
       "``view_template_data`` permission.", "Templates"),
    _S("ALGORITHM_COMPATIBILITY_MAP", {}, T.DICT,
       "``{device_algorithm: [template_algorithms...]}``. Unlisted devices accept only their own "
       "algorithm.", "Templates"),
    _S("DEFAULT_ALGORITHM_VERSION", "10", T.STR,
       "Algorithm assumed when a device does not report one.", "Templates"),
    _S("SYNC_TO_UNKNOWN_ALGORITHM_DEVICES", True, T.BOOL,
       "Push templates to devices that never reported an algorithm version.", "Templates"),
    _S("TEMPLATE_MAX_BYTES", 65536, T.INT, "Largest template accepted.", "Templates"),
    # ------------------------------------------------------------------ sync
    _S("SYNC_STRATEGY", "all", T.PATH,
       "Which devices receive an enrollee. Aliases ``all``, ``groups``, or a dotted path to "
       "``callable(enrollee) -> QuerySet[Device]`` or a ``BaseSyncStrategy`` subclass.", "Sync",
       aliases={"all": f"{P}.sync.strategies.AllDevicesStrategy",
                "groups": f"{P}.sync.strategies.DeviceGroupStrategy"}),
    _S("SYNC_UNGROUPED_TO_ALL", False, T.BOOL,
       "With the ``groups`` strategy, send enrollees without groups to every device.", "Sync"),
    _S("SYNC_ON_ENROLL", True, T.BOOL, "Fan templates out when stored.", "Sync"),
    _S("SYNC_ON_DELETE", True, T.BOOL,
       "Queue device deletes on deactivation, deletion or consent withdrawal.", "Sync"),
    _S("SYNC_ON_DEVICE_APPROVAL", True, T.BOOL,
       "Backfill all in-scope enrollees onto a newly approved device.", "Sync"),
    _S("SYNC_USERS_WITHOUT_TEMPLATES", False, T.BOOL,
       "Push the user record to devices even before any template exists.", "Sync"),
    # ------------------------------------------------------------------ commands
    _S("COMMAND_MAX_RETRIES", 5, T.INT, "Retries for a failed or unacknowledged command.",
       "Commands"),
    _S("COMMAND_RETRY_BACKOFF", timedelta(seconds=60), T.DURATION,
       "Base retry delay (doubles per attempt).", "Commands"),
    _S("COMMAND_RETRY_BACKOFF_MAX", timedelta(hours=1), T.DURATION, "Maximum retry delay.",
       "Commands"),
    _S("COMMAND_ACK_TIMEOUT", timedelta(minutes=15), T.DURATION,
       "A sent command without a result after this long is retried.", "Commands"),
    _S("COMMAND_EXPIRY",
       {"reboot": "1h", "set_time": "1h", "enroll_fingerprint": "30m", "check": "1h",
        "info": "1h", "clear_logs": "1d", "clear_data": "1d"},
       T.DICT,
       "Per command type expiry (duration or ``None``). Key ``default`` applies to unlisted "
       "types; template/user adds and deletes never expire unless listed.", "Commands"),
    # ------------------------------------------------------------------ punches
    _S("DEDUPE_WINDOW_SECONDS", 60, T.INT,
       "Repeat punches by the same PIN within this window are soft duplicates (0 disables).",
       "Punches"),
    _S("DEDUPE_WINDOW_ACTION", "flag", T.STR,
       "``flag`` stores soft duplicates with the ``duplicate`` flag (excluded from processing, "
       "keeps device/server counts reconcilable); ``skip`` drops them.", "Punches"),
    _S("DEDUPE_KEY_BUILDER", f"{P}.ingestion.dedupe.default_dedupe_key", T.PATH,
       "``callable(candidate) -> str`` building the exact-duplicate key.", "Punches"),
    _S("PUNCH_STATE_RESOLVER", "trust_device", T.PATH,
       "Resolves check-in/out. Aliases ``trust_device``, ``alternate``, ``first_last`` or a "
       "``BasePunchStateResolver`` subclass path.", "Punches",
       aliases={"trust_device": f"{P}.ingestion.state.TrustDeviceResolver",
                "alternate": f"{P}.ingestion.state.AlternateResolver",
                "first_last": f"{P}.ingestion.state.FirstLastResolver"}),
    _S("PUNCH_STATE_MAP",
       {"0": "check_in", "1": "check_out", "2": "break_out", "3": "break_in",
        "4": "overtime_in", "5": "overtime_out"},
       T.DICT, "Device status code -> punch state (used by ``trust_device``).", "Punches"),
    _S("ACCEPT_UNKNOWN_PIN_PUNCHES", True, T.BOOL,
       "Store punches whose PIN matches no enrollee (flagged ``unknown_pin``).", "Punches"),
    _S("LINK_UNKNOWN_PUNCHES_ON_ENROLL", True, T.BOOL,
       "Attach earlier unknown-PIN punches when an enrollee with that PIN is created.",
       "Punches"),
    _S("FUTURE_PUNCH_TOLERANCE", timedelta(minutes=5), T.DURATION,
       "Punches later than ``received_at`` + this are flagged ``future``.", "Punches"),
    _S("CLOCK_DRIFT_WARNING_SECONDS", 120, T.INT,
       "Device clock drift above this is flagged and signalled.", "Punches"),
    _S("LATE_SYNC_THRESHOLD", timedelta(minutes=15), T.DURATION,
       "Punches received later than this after they happened are flagged ``late_sync``.",
       "Punches"),
    _S("BACKLOG_SIGNAL_THRESHOLD", 20, T.INT,
       "Batches with at least this many new punches (or any late ones) emit "
       "``backlog_synced``.", "Punches"),
    _S("PUNCH_BULK_CHUNK_SIZE", 1000, T.INT, "Rows per ``bulk_create`` call.", "Punches"),
    _S("PUNCH_EVENT_BATCH_LIMIT", 1000, T.INT,
       "Batches larger than this emit only batch-level events (``punches_received``, "
       "``backlog_synced``) instead of one ``punch_received`` per punch. ``None`` = no limit.",
       "Punches", nullable=True),
    _S("AUTO_CORRECT_DEVICE_TIME", False, T.BOOL,
       "Queue a set-time command when drift exceeds the warning threshold.", "Punches"),
    _S("MAX_AUTO_TIME_CORRECTION", timedelta(hours=1), T.DURATION,
       "Larger drifts are only reported, never corrected automatically.", "Punches"),
    # ------------------------------------------------------------------ processing
    _S("ATTENDANCE_PROCESSOR", f"{P}.processing.default.FirstInLastOutProcessor", T.PATH,
       "``BaseAttendanceProcessor`` subclass; ``None`` disables processing entirely.",
       "Processing", nullable=True),
    _S("PROCESS_ON_INGEST", True, T.BOOL,
       "Recompute affected days after punches are ingested (via the task backend).",
       "Processing"),
    _S("DAY_BOUNDARY_RESOLVER", "calendar_day", T.PATH,
       "Maps a punch to a work date. Aliases ``calendar_day``, ``offset`` (uses "
       "``DAY_START_TIME``), ``shift_aware`` (overnight shifts from the schedule provider).",
       "Processing",
       aliases={"calendar_day": f"{P}.processing.boundaries.CalendarDayBoundary",
                "offset": f"{P}.processing.boundaries.OffsetDayBoundary",
                "shift_aware": f"{P}.processing.boundaries.ShiftAwareDayBoundary"}),
    _S("DAY_START_TIME", time(0, 0), T.TIME, "Local time a work day starts (``offset`` boundary).",
       "Processing"),
    _S("ATTENDANCE_TIMEZONE", None, T.STR,
       "Timezone used to assign work dates; ``None`` uses ``DEFAULT_DEVICE_TIMEZONE``.",
       "Processing", nullable=True),
    _S("OVERNIGHT_SHIFT_MARGIN", timedelta(hours=4), T.DURATION,
       "``shift_aware`` boundary: punches up to this long after an overnight shift ends still "
       "belong to the shift's start date.", "Processing"),
    _S("STATUS_RULES",
       [f"{P}.processing.rules.on_leave", f"{P}.processing.rules.non_working_day",
        f"{P}.processing.rules.absent", f"{P}.processing.rules.incomplete",
        f"{P}.processing.rules.late", f"{P}.processing.rules.early_leave",
        f"{P}.processing.rules.present"],
       T.PATH_LIST, "Ordered status rule callables evaluated per attendance day.", "Processing"),
    _S("ABSENCE_GENERATION_ENABLED", True, T.BOOL,
       "Allow ``fpa_generate_absences`` / the periodic task to create absent days.",
       "Processing"),
    _S("ABSENCE_CUTOFF_TIME", time(23, 59), T.TIME,
       "Absences for a date are only generated after this local time.", "Processing"),
    _S("RECOMPUTE_ON_CALENDAR_CHANGE", True, T.BOOL,
       "Recompute days when holidays, leave or schedules change.", "Processing"),
    # ------------------------------------------------------------------ calendar
    _S("CALENDAR_PROVIDER", f"{P}.calendar.providers.DefaultCalendarProvider", T.PATH,
       "``BaseCalendarProvider`` resolving day types.", "Calendar"),
    _S("HOLIDAY_PROVIDER", "chained", T.PATH,
       "``BaseHolidayProvider``. Aliases ``chained``, ``settings``, ``database``, ``holidays``.",
       "Calendar",
       aliases={"chained": f"{P}.calendar.holidays.ChainedHolidayProvider",
                "settings": f"{P}.calendar.holidays.SettingsHolidayProvider",
                "database": f"{P}.calendar.holidays.DatabaseHolidayProvider",
                "holidays": f"{P}.calendar.holidays.PythonHolidaysProvider"}),
    _S("HOLIDAY_PROVIDER_CHAIN", ["database", "settings"], T.PATH_LIST,
       "Providers combined by ``chained``, highest priority first (database overrides win).",
       "Calendar",
       aliases={"settings": f"{P}.calendar.holidays.SettingsHolidayProvider",
                "database": f"{P}.calendar.holidays.DatabaseHolidayProvider",
                "holidays": f"{P}.calendar.holidays.PythonHolidaysProvider"}),
    _S("HOLIDAYS", [], T.JSON,
       "Static holidays: ``[{\"date\": \"2026-10-01\", \"name\": \"Independence Day\", "
       "\"recurring\": true}]``.", "Calendar"),
    _S("HOLIDAYS_COUNTRY", None, T.STR, "Country code for the ``holidays`` provider (e.g. NG).",
       "Calendar", nullable=True),
    _S("HOLIDAYS_SUBDIVISION", None, T.STR, "Subdivision for the ``holidays`` provider.",
       "Calendar", nullable=True),
    _S("WEEKEND_DAYS", [5, 6], T.INT_LIST, "Weekend weekdays (Monday=0).", "Calendar"),
    _S("WEEKEND_DAYS_BY_GROUP", {}, T.DICT,
       "``{device_group_name: [weekdays]}`` overrides for enrollees in that group.", "Calendar"),
    _S("LEAVE_PROVIDER", f"{P}.calendar.providers.NullLeaveProvider", T.PATH,
       "``BaseLeaveProvider`` connecting your HR/leave system.", "Calendar"),
    _S("SCHEDULE_PROVIDER", "settings", T.PATH,
       "``BaseScheduleProvider``. Aliases ``settings`` (``DEFAULT_SCHEDULE``), ``database`` "
       "(WorkSchedule/ShiftAssignment models).", "Calendar",
       aliases={"settings": f"{P}.calendar.providers.SettingsScheduleProvider",
                "database": f"{P}.calendar.providers.DatabaseScheduleProvider"}),
    _S("DEFAULT_SCHEDULE", None, T.DICT,
       "Fixed schedule, e.g. ``{\"start\": \"09:00\", \"end\": \"17:00\", \"grace_in_minutes\": "
       "10}``. ``None`` = no expected shift (no late/early statuses).", "Calendar", nullable=True),
    _S("SCHEDULE_MODELS_ENABLED", False, T.BOOL,
       "Expose WorkSchedule/ShiftAssignment in the API and admin.", "Calendar"),
    # ------------------------------------------------------------------ time
    _S("DEFAULT_DEVICE_TIMEZONE", None, T.STR,
       "Timezone of devices without their own (everything is stored in UTC).", "Time",
       default_factory=_default_timezone),
    # ------------------------------------------------------------------ tasks
    _S("TASK_BACKEND", "sync", T.PATH,
       "Runs background work. Aliases ``sync`` (inline), ``celery``, ``django`` (Django 6 "
       "tasks), or a ``BaseTaskBackend`` path.", "Tasks",
       aliases={"sync": f"{P}.tasks.backends.SyncTaskBackend",
                "celery": f"{P}.tasks.backends.CeleryTaskBackend",
                "django": f"{P}.tasks.backends.DjangoTaskBackend"}),
    _S("TASK_BACKEND_OPTIONS", {}, T.DICT,
       "Backend options (``queue`` for Celery, ``queue_name``/``backend`` for Django tasks).",
       "Tasks"),
    # ------------------------------------------------------------------ devices
    _S("DEVICE_OFFLINE_AFTER", timedelta(minutes=5), T.DURATION,
       "A device not seen for this long is offline.", "Devices"),
    _S("DEFAULT_LOG_CAPACITY", 100000, T.INT,
       "Attendance log capacity assumed when the device does not report one.", "Devices"),
    _S("LOG_CAPACITY_WARNING_RATIO", 0.9, T.FLOAT,
       "Warn when a device's stored logs reach this fraction of capacity.", "Devices"),
    # ------------------------------------------------------------------ realtime
    _S("REALTIME_BACKEND", "none", T.PATH,
       "Broadcast backend. Aliases ``none``, ``channels``.", "Realtime",
       aliases={"none": f"{P}.realtime.backends.NullRealtimeBackend",
                "channels": f"{P}.realtime.backends.ChannelsRealtimeBackend"}),
    _S("REALTIME_GROUP_NAME_BUILDER", f"{P}.realtime.backends.default_group_names", T.PATH,
       "``callable(event, payload) -> list[str]`` of channel group names.", "Realtime"),
    _S("REALTIME_EVENTS",
       ["punch_received", "device_online", "device_offline", "backlog_synced",
        "enrollment_completed", "command_failed"],
       T.LIST, "Events broadcast to WebSocket clients.", "Realtime"),
    _S("REALTIME_CONSUMER_PERMISSION", f"{P}.realtime.backends.staff_only", T.PATH,
       "``callable(scope) -> bool`` deciding who may subscribe.", "Realtime"),
    # ------------------------------------------------------------------ webhooks
    _S("WEBHOOKS_ENABLED", False, T.BOOL, "Deliver events to webhook endpoints.", "Webhooks"),
    _S("WEBHOOK_SIGNING_SECRET", None, T.STR,
       "Default HMAC-SHA256 secret (endpoints may define their own).", "Webhooks",
       nullable=True),
    _S("WEBHOOK_TIMEOUT", 10, T.INT, "HTTP timeout (seconds).", "Webhooks"),
    _S("WEBHOOK_MAX_RETRIES", 5, T.INT, "Delivery retries.", "Webhooks"),
    _S("WEBHOOK_RETRY_BACKOFF", timedelta(seconds=60), T.DURATION,
       "Base retry delay (doubles per attempt).", "Webhooks"),
    _S("WEBHOOK_EVENTS", [], T.LIST, "Event allowlist (empty = every event).", "Webhooks"),
    _S("WEBHOOK_SENDER", f"{P}.webhooks.delivery.urllib_sender", T.PATH,
       "``callable(url, body: bytes, headers, timeout) -> (status, text)``.", "Webhooks"),
    # ------------------------------------------------------------------ API
    _S("API_URL_PREFIX", "api/fingerprint/", T.STR,
       "Prefix used by ``fingerprint_attendance.urls`` for the REST API.", "API"),
    _S("API_URL_NAMESPACE", "fingerprint_attendance_api", T.STR, "URL namespace of the API.",
       "API"),
    _S("API_PERMISSION_CLASSES", ["rest_framework.permissions.IsAdminUser"], T.PATH_LIST,
       "Default permission classes for every viewset.", "API"),
    _S("API_VIEWSET_PERMISSION_CLASSES", {}, T.DICT,
       "``{\"devices\": [...], \"punches.create_manual\": [...]}`` per viewset / action "
       "overrides.", "API"),
    _S("API_AUTHENTICATION_CLASSES", None, T.PATH_LIST,
       "Authentication classes (``None`` = DRF defaults).", "API", nullable=True),
    _S("API_PAGINATION_CLASS", f"{P}.api.pagination.StandardPagination", T.PATH,
       "Pagination class for list endpoints.", "API", nullable=True),
    _S("API_PAGE_SIZE", 50, T.INT, "Default page size.", "API"),
    _S("PUNCH_PAGINATION_CLASS", f"{P}.api.pagination.PunchCursorPagination", T.PATH,
       "Pagination class for punches (cursor based).", "API", nullable=True),
    _S("API_THROTTLE_CLASSES", None, T.PATH_LIST, "Throttle classes (``None`` = DRF defaults).",
       "API", nullable=True),
    _S("SERIALIZER_OVERRIDES", {}, T.DICT,
       "``{\"devices\": \"myapp.MyDeviceSerializer\"}`` (keys: viewset basename or "
       "``basename.action``).", "API"),
    _S("VIEWSET_OVERRIDES", {}, T.DICT, "``{\"devices\": \"myapp.MyDeviceViewSet\"}``.", "API"),
    _S("FILTERSET_OVERRIDES", {}, T.DICT,
       "``{\"punches\": \"myapp.MyPunchFilterSet\"}`` (django-filter).", "API"),
    _S("API_FILTER_BACKEND", "auto", T.STR,
       "``auto`` (django-filter when installed), ``django_filter`` or ``builtin``.", "API"),
    _S("API_EXCEPTION_HANDLER", f"{P}.api.exceptions.exception_handler", T.PATH,
       "Exception handler used by package viewsets (``None`` = DRF's configured handler).",
       "API", nullable=True),
    # ------------------------------------------------------------------ privacy
    _S("RETENTION_DELETE_TEMPLATES_AFTER_DEACTIVATION", None, T.DURATION,
       "Delete stored templates this long after deactivation (``None`` keeps them).",
       "Privacy", nullable=True),
    _S("RETENTION_PUNCHES_DAYS", None, T.INT, "Delete punches older than N days (``None`` = keep "
       "forever).", "Privacy", nullable=True),
    _S("RETENTION_EVENT_LOG_DAYS", 90, T.INT, "Delete device event logs older than N days.",
       "Privacy", nullable=True),
    _S("RETENTION_COMMANDS_DAYS", 90, T.INT, "Delete finished commands older than N days.",
       "Privacy", nullable=True),
    _S("RETENTION_WEBHOOK_DELIVERIES_DAYS", 30, T.INT, "Delete webhook deliveries older than N "
       "days.", "Privacy", nullable=True),
    _S("RETENTION_AUDIT_LOG_DAYS", None, T.INT, "Delete audit entries older than N days.",
       "Privacy", nullable=True),
    _S("AUDIT_LOG_ENABLED", True, T.BOOL, "Write AuditLog rows.", "Privacy"),
    _S("LOG_REDACTION", True, T.BOOL,
       "Redact templates, tokens and keys from package log records.", "Privacy"),
    # ------------------------------------------------------------------ hooks / events
    _S("HOOKS", {}, T.DICT,
       "``{event_name: [\"dotted.callable\", ...]}``; each is called as "
       "``hook(event, payload)``.", "Hooks"),
    _S("HOOKS_ASYNC", True, T.BOOL, "Run hooks through the task backend.", "Hooks"),
    _S("EVENTS_ON_COMMIT", True, T.BOOL,
       "Dispatch signals/hooks/webhooks after the surrounding transaction commits.", "Hooks"),
)

SPEC_BY_NAME: dict[str, Setting] = {s.name: s for s in SETTINGS_SPEC}


# --------------------------------------------------------------------------- settings object


class FPASettings:
    """Lazy, cached accessor for package settings."""

    def __init__(self, spec: Iterable[Setting] = SETTINGS_SPEC) -> None:
        self._spec = {s.name: s for s in spec}
        self._cache: dict[str, Any] = {}
        self._import_cache: dict[str, Any] = {}

    # -- plumbing -----------------------------------------------------------------
    @staticmethod
    def _user_settings() -> Mapping[str, Any]:
        from django.conf import settings as dj

        value = getattr(dj, SETTINGS_NAME, None) or {}
        if not isinstance(value, Mapping):
            raise ImproperlyConfigured(f"{SETTINGS_NAME} must be a dict")
        return value

    def reload(self) -> None:
        self._cache.clear()
        self._import_cache.clear()

    def spec(self, name: str) -> Setting:
        try:
            return self._spec[name]
        except KeyError:
            raise AttributeError(f"Unknown fingerprint_attendance setting {name!r}") from None

    def source_of(self, name: str) -> str:
        """Where a setting's value comes from: ``settings``, ``env`` or ``default``."""
        spec = self.spec(name)
        if name in self._user_settings():
            return "settings"
        if spec.env_var in os.environ:
            return "env"
        return "default"

    def raw(self, name: str) -> Any:
        spec = self.spec(name)
        user = self._user_settings()
        if name in user:
            value, from_env = user[name], False
        elif spec.env_var in os.environ:
            value, from_env = os.environ[spec.env_var], True
        else:
            return spec.get_default()
        if value is None or (
            from_env and spec.nullable and str(value).strip().lower() in _NULL
        ):
            if spec.nullable:
                return None
            if not from_env:
                return spec.get_default()
        try:
            return cast_value(value, spec.type, from_env=from_env)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            source = spec.env_var if from_env else f"{SETTINGS_NAME}[{name!r}]"
            raise ImproperlyConfigured(f"Invalid value for {source}: {exc}") from exc

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        cache = self.__dict__["_cache"]
        if name not in cache:
            cache[name] = self.raw(name)
        return cache[name]

    # -- dotted paths -------------------------------------------------------------------
    def resolve_path(self, name: str, value: Any = _MISSING) -> Any:
        """Expand an alias to its dotted path (objects are returned unchanged)."""
        spec = self.spec(name)
        if value is _MISSING:
            value = getattr(self, name)
        if isinstance(value, str) and value in spec.aliases:
            return spec.aliases[value]
        return value

    def import_(self, name: str) -> Any:
        """Import the object a dotted-path setting refers to (``None`` if unset)."""
        spec = self.spec(name)
        if not spec.is_import:
            raise ImproperlyConfigured(f"{name} is not an import setting")
        if name in self._import_cache:
            return self._import_cache[name]
        value = getattr(self, name)
        if value is None:
            result: Any = None
        elif spec.type == SettingType.PATH_LIST:
            result = [import_object(self.resolve_path(name, v), setting=name) for v in value]
        else:
            result = import_object(self.resolve_path(name, value), setting=name)
        self._import_cache[name] = result
        return result

    def get_duration_map(self, name: str) -> dict[str, timedelta | None]:
        """Read a ``{key: duration}`` mapping setting, casting each value."""
        out: dict[str, timedelta | None] = {}
        for key, value in (getattr(self, name) or {}).items():
            out[str(key)] = None if value is None else parse_duration_value(value)
        return out

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._spec))


def import_object(value: Any, *, setting: str | None = None) -> Any:
    """Import ``value`` if it is a dotted path; return it unchanged otherwise."""
    if not isinstance(value, str):
        return value
    try:
        return import_string(value)
    except ImportError as exc:
        where = f" (setting {setting})" if setting else ""
        raise ImproperlyConfigured(f"Could not import {value!r}{where}: {exc}") from exc


settings = FPASettings()


def _on_setting_changed(*, setting: str, **kwargs: Any) -> None:
    if setting in (SETTINGS_NAME, "AUTH_USER_MODEL", "TIME_ZONE"):
        settings.reload()


setting_changed.connect(_on_setting_changed, dispatch_uid="fpa_settings_reload")


def employee_model_label() -> str:
    """The ``app_label.Model`` string enrollees link to."""
    return str(settings.EMPLOYEE_MODEL)


def get_employee_model() -> Any:
    from django.apps import apps

    return apps.get_model(employee_model_label(), require_ready=False)
