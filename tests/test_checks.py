from __future__ import annotations

import pytest

from fingerprint_attendance.checks import check_settings


def ids(fpa, **values):
    fpa(**values)
    return {m.id for m in check_settings()}


def test_clean_configuration_passes(fpa):
    assert ids(fpa) == set()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"PIN_GENERATOR": "does.not.exist"}, "fpa.E001"),
        ({"TEMPLATE_ENCRYPTION_KEYS": []}, "fpa.E002"),
        ({"TEMPLATE_ENCRYPTION_KEYS": ["not-a-key"]}, "fpa.E003"),
        ({"EMPLOYEE_MODEL": "nope.Model"}, "fpa.E005"),
        ({"DEFAULT_DEVICE_TIMEZONE": "Mars/Olympus"}, "fpa.E006"),
        ({"FINGERS_REQUIRED_MIN": 5, "FINGERS_ALLOWED_MAX": 2}, "fpa.E007"),
        ({"FINGERS_REQUIRED_MIN": 3, "ALLOWED_FINGER_INDEXES": [1, 2]}, "fpa.E007"),
        ({"ALLOWED_FINGER_INDEXES": [0, 12]}, "fpa.E008"),
        ({"ADMS_DELAY": "soon"}, "fpa.E012"),
        ({"DEDUPE_WINDOW_ACTION": "drop"}, "fpa.E013"),
        ({"API_FILTER_BACKEND": "magic"}, "fpa.E014"),
        ({"ADMS_URL_PREFIX": "/iclock"}, "fpa.E015"),
        ({"ADMS_ALLOWED_IPS": ["not-an-ip"]}, "fpa.E016"),
        ({"ADMS_RATE_LIMIT": "fast"}, "fpa.E017"),
        ({"WEEKEND_DAYS": [7]}, "fpa.E018"),
        ({"COMMAND_EXPIRY": {"reboot": "whenever"}}, "fpa.E020"),
        ({"HOOKS": {"punch_received": ["missing.hook"]}}, "fpa.E021"),
        ({"HOOKS": {"punch_received": 5}}, "fpa.E021"),
        ({"DEFAULT_SCHEDULE": {"start": "nine"}}, "fpa.E022"),
        ({"PIN_MAX_LENGTH": 0}, "fpa.E023"),
        ({"HOLIDAY_PROVIDER_CHAIN": ["holidays"]}, "fpa.E024"),
        ({"TEMPLATE_ENCRYPTION_ENABLED": False}, "fpa.W001"),
        ({"TYPO_SETTING": 1}, "fpa.W002"),
        ({"WEBHOOKS_ENABLED": True}, "fpa.W003"),
        ({"ADMS_DEVICE_TOKEN_REQUIRED": True}, "fpa.W004"),
    ],
)
def test_errors(fpa, values, expected):
    assert expected in ids(fpa, **values)


def test_use_tz_required(fpa, settings):
    settings.USE_TZ = False
    assert "fpa.E004" in {m.id for m in check_settings()}


def test_missing_extras(fpa, monkeypatch):
    import fingerprint_attendance.checks as checks

    monkeypatch.setattr(checks, "_has_module", lambda name: False)
    found = ids(fpa, TASK_BACKEND="celery", REALTIME_BACKEND="channels", PULL_ENABLED=True,
                API_FILTER_BACKEND="django_filter", HOLIDAY_PROVIDER="holidays",
                HOLIDAYS_COUNTRY="NG")
    assert {"fpa.E009", "fpa.E010", "fpa.E019", "fpa.E014", "fpa.E024"} <= found
    assert "fpa.E009" in ids(fpa, TASK_BACKEND="django", REALTIME_BACKEND="none",
                             PULL_ENABLED=False, API_FILTER_BACKEND="auto",
                             HOLIDAY_PROVIDER="chained")


def test_holidays_provider_with_country_is_fine(fpa):
    assert "fpa.E024" not in ids(fpa, HOLIDAY_PROVIDER_CHAIN=["database", "holidays"],
                                 HOLIDAYS_COUNTRY="NG")


def test_non_dict_settings(settings):
    settings.FINGERPRINT_ATTENDANCE = "oops"
    assert {m.id for m in check_settings()} >= {"fpa.E000"}
