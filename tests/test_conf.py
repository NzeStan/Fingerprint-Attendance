from __future__ import annotations

from datetime import time, timedelta

import pytest
from django.core.exceptions import ImproperlyConfigured

from fingerprint_attendance.conf import (
    SETTINGS_SPEC,
    FPASettings,
    cast_value,
    parse_duration_value,
    parse_time_value,
    settings,
)


def test_defaults_are_used(fpa):
    assert settings.ADMS_URL_PREFIX == "iclock/"
    assert settings.source_of("ADMS_URL_PREFIX") == "default"


def test_precedence_settings_over_env_over_default(fpa, monkeypatch):
    monkeypatch.setenv("FPA_ADMS_DELAY", "42")
    settings.reload()
    assert settings.ADMS_DELAY == 42
    assert settings.source_of("ADMS_DELAY") == "env"
    fpa(ADMS_DELAY=7)
    assert settings.ADMS_DELAY == 7
    assert settings.source_of("ADMS_DELAY") == "settings"


def test_reset_on_setting_changed(fpa):
    assert settings.DEDUPE_WINDOW_SECONDS == 60
    fpa(DEDUPE_WINDOW_SECONDS=5)
    assert settings.DEDUPE_WINDOW_SECONDS == 5


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("ADMS_ENABLED", "false", False),
        ("ADMS_ENABLED", "YES", True),
        ("ADMS_DELAY", "15", 15),
        ("LOG_CAPACITY_WARNING_RATIO", "0.75", 0.75),
        ("ADMS_ALLOWED_IPS", "10.0.0.0/8, 192.168.1.5", ["10.0.0.0/8", "192.168.1.5"]),
        ("ADMS_ALLOWED_IPS", '["1.2.3.4"]', ["1.2.3.4"]),
        ("WEEKEND_DAYS", "4,5", [4, 5]),
        ("HOOKS", '{"punch_received": ["a.b"]}', {"punch_received": ["a.b"]}),
        ("DEVICE_OFFLINE_AFTER", "90s", timedelta(seconds=90)),
        ("DEVICE_OFFLINE_AFTER", "2h", timedelta(hours=2)),
        ("DEVICE_OFFLINE_AFTER", "01:30:00", timedelta(hours=1, minutes=30)),
        ("DAY_START_TIME", "04:30", time(4, 30)),
        ("RETENTION_PUNCHES_DAYS", "none", None),
        ("HOLIDAYS", '[{"date": "2026-10-01", "name": "Independence"}]',
         [{"date": "2026-10-01", "name": "Independence"}]),
    ],
)
def test_env_casting(monkeypatch, name, raw, expected):
    monkeypatch.setenv(f"FPA_{name}", raw)
    settings.reload()
    assert getattr(settings, name) == expected


def test_invalid_env_value_raises(monkeypatch):
    monkeypatch.setenv("FPA_ADMS_DELAY", "soon")
    settings.reload()
    with pytest.raises(ImproperlyConfigured, match="FPA_ADMS_DELAY"):
        _ = settings.ADMS_DELAY


def test_unknown_setting_raises():
    with pytest.raises(AttributeError):
        _ = settings.NOT_A_SETTING


def test_dict_values_are_cast(fpa):
    fpa(DEVICE_OFFLINE_AFTER=120, ALLOWED_FINGER_INDEXES=["1", "6"])
    assert timedelta(minutes=2) == settings.DEVICE_OFFLINE_AFTER
    assert settings.ALLOWED_FINGER_INDEXES == [1, 6]


def test_none_in_settings_for_non_nullable_falls_back_to_default(fpa):
    fpa(ADMS_DELAY=None)
    assert settings.ADMS_DELAY == 10


def test_aliases_and_import(fpa):
    from fingerprint_attendance.tasks.backends import CeleryTaskBackend, SyncTaskBackend

    assert settings.import_("TASK_BACKEND") is SyncTaskBackend
    fpa(TASK_BACKEND="celery")
    assert settings.import_("TASK_BACKEND") is CeleryTaskBackend
    fpa(TASK_BACKEND=SyncTaskBackend)  # objects are accepted as-is
    assert settings.import_("TASK_BACKEND") is SyncTaskBackend


def test_import_errors_are_improperly_configured(fpa):
    fpa(PIN_GENERATOR="nope.missing")
    with pytest.raises(ImproperlyConfigured, match="PIN_GENERATOR"):
        settings.import_("PIN_GENERATOR")
    with pytest.raises(ImproperlyConfigured):
        settings.import_("ADMS_DELAY")


def test_default_factories(settings):
    assert FPASettings().EMPLOYEE_MODEL == "testapp.Employee"
    settings.FINGERPRINT_ATTENDANCE = {"TEMPLATE_ENCRYPTION_KEYS": []}
    assert FPASettings().EMPLOYEE_MODEL == "auth.User"
    assert FPASettings().DEFAULT_DEVICE_TIMEZONE == "Africa/Lagos"


def test_path_list_import(fpa):
    rules = settings.import_("STATUS_RULES")
    assert callable(rules[0])


def test_duration_map(fpa):
    fpa(COMMAND_EXPIRY={"reboot": "10m", "default": None, "x": 30})
    mapping = settings.get_duration_map("COMMAND_EXPIRY")
    assert mapping == {"reboot": timedelta(minutes=10), "default": None,
                       "x": timedelta(seconds=30)}


def test_parse_helpers():
    assert parse_duration_value("1.5m") == timedelta(seconds=90)
    assert parse_duration_value("P1D") == timedelta(days=1)
    assert parse_time_value("07:15:30") == time(7, 15, 30)
    with pytest.raises(ValueError):
        parse_duration_value(True)
    with pytest.raises(ValueError):
        parse_duration_value("later")
    with pytest.raises(ValueError):
        parse_time_value(5)
    with pytest.raises(ValueError):
        cast_value("maybe", "bool")
    with pytest.raises(ValueError):
        cast_value(True, "int")
    with pytest.raises(ValueError):
        cast_value("[1]", "dict")
    assert cast_value('{"a": 1}', "list") == ['{"a": 1}']  # non-JSON strings are comma split
    with pytest.raises(ValueError):
        cast_value('[1', "list")
    assert cast_value("not json", "json", from_env=True) == "not json"


def test_every_setting_is_documented():
    names = [s.name for s in SETTINGS_SPEC]
    assert len(names) == len(set(names)), "duplicate setting names"
    for spec in SETTINGS_SPEC:
        assert spec.description, spec.name
        assert spec.env_var == f"FPA_{spec.name}"
        spec.display_default()  # must not fail


def test_settings_dict_must_be_mapping(settings):
    settings.FINGERPRINT_ATTENDANCE = ["nope"]
    with pytest.raises(ImproperlyConfigured):
        _ = FPASettings().ADMS_DELAY


def test_dir_lists_settings():
    assert "ADMS_URL_PREFIX" in dir(settings)


def test_docs_reference_is_in_sync():
    """docs/configuration.md is generated from SETTINGS_SPEC (scripts/gen_settings_docs.py)."""
    from pathlib import Path

    from scripts.gen_settings_docs import render

    path = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"
    assert path.read_text(encoding="utf-8") == render(), (
        "run: python scripts/gen_settings_docs.py")
