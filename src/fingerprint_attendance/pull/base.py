"""Pull-mode adapter interface (devices on a LAN that cannot push)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class PulledUser:
    pin: str
    name: str = ""
    privilege: int = 0
    uid: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PulledTemplate:
    pin: str
    finger_index: int
    template: bytes
    valid: bool = True
    algorithm_version: str | None = None


@dataclass
class PulledPunch:
    pin: str
    local_time: datetime  # naive, device local time
    status: str = ""
    verify: int | None = None
    work_code: str = ""
    raw: str = ""


@dataclass
class PulledDeviceInfo:
    serial_number: str = ""
    firmware_version: str = ""
    platform: str = ""
    fp_algorithm_version: str = ""
    user_count: int | None = None
    template_count: int | None = None
    punch_count: int | None = None
    punch_capacity: int | None = None
    device_time: datetime | None = None


class PullError(Exception):
    """Connection or protocol failure talking to a device."""


class BasePullAdapter:
    """Implement for your SDK. The default is :class:`PyZKPullAdapter`.

    Adapters are used as context managers::

        with adapter_for(device) as conn:
            punches = conn.get_attendance()
    """

    def __init__(self, device: Any) -> None:
        self.device = device

    def __enter__(self) -> BasePullAdapter:
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()

    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def get_info(self) -> PulledDeviceInfo:
        raise NotImplementedError

    def get_users(self) -> list[PulledUser]:
        raise NotImplementedError

    def get_templates(self) -> list[PulledTemplate]:
        raise NotImplementedError

    def get_attendance(self) -> list[PulledPunch]:
        raise NotImplementedError

    def set_user(self, user: PulledUser) -> None:
        raise NotImplementedError

    def set_templates(self, user: PulledUser, templates: list[PulledTemplate]) -> None:
        raise NotImplementedError

    def delete_user(self, pin: str) -> None:
        raise NotImplementedError

    def set_time(self, local_time: datetime) -> None:
        raise NotImplementedError

    def live_capture(self, timeout: int = 10) -> Iterator[PulledPunch | None]:
        """Yield punches as they happen; yield ``None`` on idle timeouts so callers can check
        for shutdown."""
        raise NotImplementedError


def adapter_for(device: Any) -> BasePullAdapter:
    from ..conf import settings

    cls = settings.import_("PULL_ADAPTER")
    return cls(device)
