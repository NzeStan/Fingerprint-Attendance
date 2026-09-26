"""An in-memory pull adapter for tests (no network, no pyzk)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from ..pull.base import (
    BasePullAdapter,
    PulledDeviceInfo,
    PulledPunch,
    PulledTemplate,
    PulledUser,
    PullError,
)


class FakeTerminal:
    """State shared by every adapter instance created for one device."""

    def __init__(self, serial: str = "PULL001") -> None:
        self.serial = serial
        self.users: dict[str, PulledUser] = {}
        self.templates: dict[tuple[str, int], PulledTemplate] = {}
        self.punches: list[PulledPunch] = []
        self.live_queue: list[PulledPunch | None] = []
        self.clock: datetime | None = None
        self.fail_connects = 0
        self.connects = 0


class FakePullAdapter(BasePullAdapter):
    terminals: dict[str, FakeTerminal] = {}

    def __init__(self, device: Any) -> None:
        super().__init__(device)
        self.terminal = self.terminals.setdefault(device.serial_number,
                                                  FakeTerminal(device.serial_number))
        self.connected = False

    @classmethod
    def reset(cls) -> None:
        cls.terminals = {}

    def connect(self) -> None:
        self.terminal.connects += 1
        if self.terminal.fail_connects > 0:
            self.terminal.fail_connects -= 1
            raise PullError("simulated connection failure")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def get_info(self) -> PulledDeviceInfo:
        return PulledDeviceInfo(serial_number=self.terminal.serial, firmware_version="FAKE",
                                fp_algorithm_version="10", user_count=len(self.terminal.users),
                                template_count=len(self.terminal.templates),
                                punch_count=len(self.terminal.punches), punch_capacity=100000,
                                device_time=self.terminal.clock)

    def get_users(self) -> list[PulledUser]:
        return list(self.terminal.users.values())

    def get_templates(self) -> list[PulledTemplate]:
        return list(self.terminal.templates.values())

    def get_attendance(self) -> list[PulledPunch]:
        return list(self.terminal.punches)

    def set_user(self, user: PulledUser) -> None:
        self.terminal.users[user.pin] = user

    def set_templates(self, user: PulledUser, templates: list[PulledTemplate]) -> None:
        for t in templates:
            self.terminal.templates[(user.pin, t.finger_index)] = t

    def delete_user(self, pin: str) -> None:
        self.terminal.users.pop(pin, None)
        for key in [k for k in self.terminal.templates if k[0] == pin]:
            self.terminal.templates.pop(key)

    def set_time(self, local_time: datetime) -> None:
        self.terminal.clock = local_time

    def live_capture(self, timeout: int = 10) -> Iterator[PulledPunch | None]:
        while self.terminal.live_queue:
            item = self.terminal.live_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            if item is not None:
                self.terminal.punches.append(item)
            yield item
        raise PullError("simulated disconnect (live queue drained)")
