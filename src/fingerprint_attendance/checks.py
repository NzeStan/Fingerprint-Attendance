"""Django system checks validating the package configuration.

Error IDs are stable and documented (``fpa.E0xx`` errors, ``fpa.W0xx`` warnings).
"""

from __future__ import annotations

import importlib.util
import ipaddress
from typing import Any

from django.apps import apps
from django.conf import settings as dj_settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured

from .conf import SETTINGS_NAME, SETTINGS_SPEC, SPEC_BY_NAME, settings
from .utils.ratelimit import parse_rate
from .utils.timeutils import get_zone

TAG = "fingerprint_attendance"


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _error(msg: str, id_: str, hint: str | None = None) -> checks.Error:
    return checks.Error(msg, hint=hint, id=f"fpa.{id_}")


def _warning(msg: str, id_: str, hint: str | None = None) -> checks.Warning:
    return checks.Warning(msg, hint=hint, id=f"fpa.{id_}")


@checks.register(TAG)
def check_settings(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    messages: list[checks.CheckMessage] = []

    # fpa.E012 -- every value must cast to its declared type.
    valid: dict[str, Any] = {}
    for spec in SETTINGS_SPEC:
        try:
            valid[spec.name] = settings.raw(spec.name)
        except ImproperlyConfigured as exc:
            messages.append(_error(str(exc), "E012", f"See the configuration reference for "
                                                       f"{spec.name}."))
    # fpa.W002 -- unknown keys are almost always typos.
    user = getattr(dj_settings, SETTINGS_NAME, None) or {}
    if not isinstance(user, dict):
        return [_error(f"{SETTINGS_NAME} must be a dict.", "E000")]
    for key in user:
        if key not in SPEC_BY_NAME:
            messages.append(_warning(f"Unknown setting {SETTINGS_NAME}[{key!r}] is ignored.",
                                     "W002", "Check the spelling against the reference table."))

    def get(name: str) -> Any:
        return valid.get(name)

    # fpa.E001 -- dotted paths must import.
    optional_extras = {
        "PULL_ADAPTER": "PULL_ENABLED",
    }
    for spec in SETTINGS_SPEC:
        if not spec.is_import or spec.name not in valid or valid[spec.name] is None:
            continue
        gate = optional_extras.get(spec.name)
        if gate and not get(gate):
            continue
        try:
            settings.import_(spec.name)
        except ImproperlyConfigured as exc:
            messages.append(_error(str(exc), "E001"))

    # fpa.E004 -- timezone-aware datetimes are required.
    if not getattr(dj_settings, "USE_TZ", False):
        messages.append(_error("USE_TZ must be True: punches are stored in UTC.", "E004"))

    # fpa.E005 -- the employee model must exist.
    label = get("EMPLOYEE_MODEL")
    if label:
        try:
            apps.get_model(label)
        except (LookupError, ValueError):
            messages.append(_error(f"EMPLOYEE_MODEL {label!r} is not an installed model.", "E005",
                                   "Use the 'app_label.ModelName' form."))

    # fpa.E002 / E003 / W001 -- encryption.
    if get("TEMPLATE_ENCRYPTION_ENABLED"):
        keys = get("TEMPLATE_ENCRYPTION_KEYS") or []
        if not keys:
            messages.append(_error(
                "TEMPLATE_ENCRYPTION_ENABLED is on but TEMPLATE_ENCRYPTION_KEYS is empty.", "E002",
                "Generate a key with `python manage.py fpa_generate_key` and set "
                "FPA_TEMPLATE_ENCRYPTION_KEYS (or disable encryption, not recommended)."))
        else:
            from cryptography.fernet import Fernet

            for i, key in enumerate(keys):
                try:
                    Fernet(key)
                except (ValueError, TypeError):
                    messages.append(_error(
                        f"TEMPLATE_ENCRYPTION_KEYS[{i}] is not a valid Fernet key.", "E003"))
    elif "TEMPLATE_ENCRYPTION_ENABLED" in valid:
        messages.append(_warning(
            "Fingerprint templates are stored unencrypted.", "W001",
            "Enable TEMPLATE_ENCRYPTION_ENABLED; biometric data is sensitive personal data."))

    # fpa.E006 -- timezones.
    for name in ("DEFAULT_DEVICE_TIMEZONE", "ATTENDANCE_TIMEZONE"):
        value = get(name)
        if value:
            try:
                get_zone(value)
            except Exception:
                messages.append(_error(f"{name} {value!r} is not a valid IANA timezone.", "E006"))

    # fpa.E007 / E008 -- enrollment limits.
    allowed = get("ALLOWED_FINGER_INDEXES") or []
    bad_idx = [i for i in allowed if not 0 <= int(i) <= 9]
    if bad_idx:
        messages.append(_error(f"ALLOWED_FINGER_INDEXES contains invalid indexes {bad_idx}.",
                               "E008", "Finger indexes are 0-9."))
    mn, mx = get("FINGERS_REQUIRED_MIN"), get("FINGERS_ALLOWED_MAX")
    if mn is not None and mx is not None:
        if mn < 0 or mx < 1 or mx > 10 or mn > mx:
            messages.append(_error(
                f"Invalid finger limits: FINGERS_REQUIRED_MIN={mn}, FINGERS_ALLOWED_MAX={mx}.",
                "E007", "Require 0 <= min <= max <= 10."))
        elif allowed and mn > len(set(allowed)):
            messages.append(_error(
                "FINGERS_REQUIRED_MIN is larger than the number of ALLOWED_FINGER_INDEXES.",
                "E007"))

    # fpa.E009 / E010 / E019 -- optional extras.
    task_backend = settings.resolve_path("TASK_BACKEND", get("TASK_BACKEND"))
    if isinstance(task_backend, str) and task_backend.endswith("CeleryTaskBackend") and not (
        _has_module("celery")
    ):
        messages.append(_error("TASK_BACKEND is 'celery' but Celery is not installed.", "E009",
                               "pip install 'django-fingerprint-attendance[celery]'"))
    if isinstance(task_backend, str) and task_backend.endswith("DjangoTaskBackend") and not (
        _has_module("django.tasks")
    ):
        messages.append(_error("TASK_BACKEND is 'django' but django.tasks needs Django 6.0+.",
                               "E009"))
    realtime = settings.resolve_path("REALTIME_BACKEND", get("REALTIME_BACKEND"))
    if isinstance(realtime, str) and realtime.endswith("ChannelsRealtimeBackend") and not (
        _has_module("channels")
    ):
        messages.append(_error("REALTIME_BACKEND is 'channels' but Channels is not installed.",
                               "E010", "pip install 'django-fingerprint-attendance[channels]'"))
    if get("PULL_ENABLED"):
        adapter = get("PULL_ADAPTER")
        if isinstance(adapter, str) and adapter.endswith("PyZKPullAdapter") and not (
            _has_module("zk")
        ):
            messages.append(_error("PULL_ENABLED with the pyzk adapter but pyzk is not installed.",
                                   "E019", "pip install 'django-fingerprint-attendance[pull]'"))

    # fpa.W003 -- webhooks.
    if get("WEBHOOKS_ENABLED") and not get("WEBHOOK_SIGNING_SECRET"):
        messages.append(_warning(
            "WEBHOOKS_ENABLED without WEBHOOK_SIGNING_SECRET: endpoints without their own secret "
            "will receive unsigned requests.", "W003"))

    # fpa.E013 / E014 -- enumerations.
    if get("DEDUPE_WINDOW_ACTION") not in (None, "flag", "skip"):
        messages.append(_error("DEDUPE_WINDOW_ACTION must be 'flag' or 'skip'.", "E013"))
    fb = get("API_FILTER_BACKEND")
    if fb not in (None, "auto", "django_filter", "builtin"):
        messages.append(_error("API_FILTER_BACKEND must be auto, django_filter or builtin.",
                               "E014"))
    elif fb == "django_filter" and not _has_module("django_filters"):
        messages.append(_error("API_FILTER_BACKEND is django_filter but django-filter is not "
                               "installed.", "E014"))

    # fpa.E015 -- URL prefixes.
    for name in ("ADMS_URL_PREFIX", "API_URL_PREFIX"):
        prefix = get(name)
        if prefix is not None and (prefix.startswith("/") or (prefix and not prefix.endswith("/"))):
            messages.append(_error(f"{name} must not start with '/' and must end with '/'.",
                                   "E015", f"e.g. {name}='iclock/'"))

    # fpa.E016 -- IP allowlists.
    for name in ("ADMS_ALLOWED_IPS", "ADMS_TRUSTED_PROXY_IPS"):
        for entry in get(name) or []:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                messages.append(_error(f"{name} entry {entry!r} is not an IP or CIDR network.",
                                       "E016"))

    # fpa.E017 -- rate limits.
    for name in ("ADMS_RATE_LIMIT", "AGENT_RATE_LIMIT"):
        value = get(name)
        if value:
            try:
                parse_rate(value)
            except ValueError as exc:
                messages.append(_error(f"{name}: {exc}", "E017", "Use e.g. '600/m'."))

    # fpa.E018 -- weekend days.
    weekend = list(get("WEEKEND_DAYS") or [])
    for days in (get("WEEKEND_DAYS_BY_GROUP") or {}).values():
        weekend.extend(days if isinstance(days, list) else [days])
    try:
        if any(not 0 <= int(d) <= 6 for d in weekend):
            raise ValueError
    except (TypeError, ValueError):
        messages.append(_error("Weekend days must be integers 0 (Monday) to 6 (Sunday).",
                               "E018"))

    # fpa.E020 -- command expiry durations.
    try:
        settings.get_duration_map("COMMAND_EXPIRY")
    except (ValueError, TypeError) as exc:
        messages.append(_error(f"COMMAND_EXPIRY: {exc}", "E020"))

    # fpa.E021 -- hooks.
    hooks = get("HOOKS") or {}
    from .conf import import_object

    for event, paths in hooks.items():
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, (list, tuple)):
            messages.append(_error(f"HOOKS[{event!r}] must be a list of dotted paths.", "E021"))
            continue
        for path in paths:
            try:
                import_object(path, setting="HOOKS")
            except ImproperlyConfigured as exc:
                messages.append(_error(str(exc), "E021"))

    # fpa.E022 -- default schedule.
    schedule = get("DEFAULT_SCHEDULE")
    if schedule:
        from .calendar.types import ShiftInfo

        try:
            ShiftInfo.from_config(schedule)
        except (KeyError, ValueError, TypeError) as exc:
            messages.append(_error(f"DEFAULT_SCHEDULE is invalid: {exc}", "E022",
                                   "Expected {'start': 'HH:MM', 'end': 'HH:MM', ...}."))

    # fpa.E023 -- pins.
    if (get("PIN_MAX_LENGTH") or 0) < 1:
        messages.append(_error("PIN_MAX_LENGTH must be at least 1.", "E023"))

    # fpa.W004 -- token + auto registration.
    if get("ADMS_DEVICE_TOKEN_REQUIRED") and get("ADMS_AUTO_REGISTER_DEVICES"):
        messages.append(_warning(
            "ADMS_DEVICE_TOKEN_REQUIRED with ADMS_AUTO_REGISTER_DEVICES: unknown devices cannot "
            "present a token, so auto-registration will never succeed.", "W004",
            "Register devices (and issue tokens) before connecting them."))

    # fpa.E024 -- python-holidays.
    chain = [settings.resolve_path("HOLIDAY_PROVIDER_CHAIN", p)
             for p in (get("HOLIDAY_PROVIDER_CHAIN") or [])]
    chain.append(settings.resolve_path("HOLIDAY_PROVIDER", get("HOLIDAY_PROVIDER")))
    if any(isinstance(p, str) and p.endswith("PythonHolidaysProvider") for p in chain):
        if not _has_module("holidays"):
            messages.append(_error("The 'holidays' provider needs the holidays package.", "E024",
                                   "pip install 'django-fingerprint-attendance[holidays]'"))
        elif not get("HOLIDAYS_COUNTRY"):
            messages.append(_error("The 'holidays' provider needs HOLIDAYS_COUNTRY.", "E024"))

    return messages
