from __future__ import annotations

import itertools
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from django.test import Client
from rest_framework.test import APIClient

from fingerprint_attendance import services
from fingerprint_attendance.constants import DeviceStatus
from fingerprint_attendance.models import Device, Enrollee
from fingerprint_attendance.testing import ADMSDeviceSimulator, FakePullAdapter
from tests.testapp.models import Employee

_counter = itertools.count(1)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("FPA_"):
            monkeypatch.delenv(key, raising=False)
    FakePullAdapter.reset()
    from django.core.cache import cache

    cache.clear()


@pytest.fixture
def fpa(settings: Any) -> Callable[..., None]:
    """``fpa(KEY=value, ...)`` overrides package settings for the test."""

    def update(**values: Any) -> None:
        settings.FINGERPRINT_ATTENDANCE = {**settings.FINGERPRINT_ATTENDANCE, **values}

    return update


@pytest.fixture
def make_employee(db: Any) -> Callable[..., Employee]:
    def factory(name: str | None = None, **kwargs: Any) -> Employee:
        n = next(_counter)
        return Employee.objects.create(staff_number=kwargs.pop("staff_number", f"S{n:04d}"),
                                       full_name=name or f"Employee {n}", **kwargs)

    return factory


@pytest.fixture
def make_enrollee(make_employee: Callable[..., Employee]) -> Callable[..., Enrollee]:
    def factory(*, consent: bool = True, pin: str | None = None, groups: Any = (),
                **kwargs: Any) -> Enrollee:
        enrollee = services.create_enrollee(make_employee(**kwargs), device_pin=pin,
                                            groups=groups)
        if consent:
            services.give_consent(enrollee, version="v1", method="digital")
            enrollee.refresh_from_db()
        return enrollee

    return factory


@pytest.fixture
def make_device(db: Any) -> Callable[..., Device]:
    def factory(serial: str | None = None, *, status: str = DeviceStatus.ACTIVE,
                algorithm: str = "10", push_version: str = "2.2.14", **kwargs: Any) -> Device:
        return Device.objects.create(
            serial_number=serial or f"DEV{next(_counter):04d}", status=status,
            fp_algorithm_version=algorithm, push_version=push_version, **kwargs)

    return factory


@pytest.fixture
def admin_user(django_user_model: Any) -> Any:
    return django_user_model.objects.create_superuser("admin", "admin@example.com", "pw")


@pytest.fixture
def staff_user(django_user_model: Any) -> Any:
    return django_user_model.objects.create_user("staff", "staff@example.com", "pw",
                                                 is_staff=True)


@pytest.fixture
def api(admin_user: Any) -> APIClient:
    client = APIClient()
    client.force_authenticate(admin_user)
    return client


@pytest.fixture
def simulator(db: Any) -> Callable[..., ADMSDeviceSimulator]:
    def factory(serial: str | None = None, *, firmware: str = "push2", approve: bool = True,
                **kwargs: Any) -> ADMSDeviceSimulator:
        sim = ADMSDeviceSimulator(Client(), serial=serial or f"SIM{next(_counter):04d}",
                                  firmware=firmware, **kwargs)
        sim.handshake()
        if approve:
            services.approve_device(Device.objects.get(serial_number=sim.serial))
        return sim

    return factory


def local(*args: int) -> datetime:
    """Naive device-local datetime helper."""
    return datetime(*args)  # noqa: DTZ001
