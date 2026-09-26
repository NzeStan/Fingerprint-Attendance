"""Pull adapter using ``pyzk`` (``[pull]`` extra). ``zk`` is imported lazily so the package
works without it."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from ..conf import settings
from .base import (
    BasePullAdapter,
    PulledDeviceInfo,
    PulledPunch,
    PulledTemplate,
    PulledUser,
    PullError,
)


class PyZKPullAdapter(BasePullAdapter):
    #: pyzk reads/writes ZKFinger v10 templates over the TCP/UDP protocol
    algorithm_version = "10"

    def __init__(self, device: Any) -> None:
        super().__init__(device)
        self.conn: Any = None
        self._uids: dict[str, int] = {}

    def _zk(self) -> Any:
        try:
            from zk import ZK
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise PullError("pyzk is not installed: pip install "
                            "'django-fingerprint-attendance[pull]'") from exc
        device = self.device
        if not device.pull_host:
            raise PullError(f"device {device.serial_number} has no pull_host")
        return ZK(device.pull_host, port=device.pull_port or settings.PULL_DEFAULT_PORT,
                  timeout=settings.PULL_TIMEOUT,
                  password=device.pull_comm_key if device.pull_comm_key is not None
                  else settings.PULL_COMM_KEY,
                  force_udp=settings.PULL_FORCE_UDP, ommit_ping=True)

    def connect(self) -> None:
        try:
            self.conn = self._zk().connect()
        except Exception as exc:
            raise PullError(f"cannot connect to {self.device.pull_host}: {exc}") from exc

    def disconnect(self) -> None:
        if self.conn is not None:
            with contextlib.suppress(Exception):  # best effort
                self.conn.enable_device()
            try:
                self.conn.disconnect()
            finally:
                self.conn = None

    def _locked(self) -> Any:
        adapter = self

        class _Lock:
            def __enter__(self) -> None:
                if settings.PULL_DISABLE_DEVICE_DURING_SYNC:
                    adapter.conn.disable_device()

            def __exit__(self, *exc: Any) -> None:
                if settings.PULL_DISABLE_DEVICE_DURING_SYNC:
                    adapter.conn.enable_device()

        return _Lock()

    def get_info(self) -> PulledDeviceInfo:
        c = self.conn
        info = PulledDeviceInfo()
        for attr, getter in (("serial_number", "get_serialnumber"),
                             ("firmware_version", "get_firmware_version"),
                             ("platform", "get_platform"),
                             ("fp_algorithm_version", "get_fp_version")):
            try:
                setattr(info, attr, str(getattr(c, getter)()))
            except Exception:  # noqa: S112 - optional capabilities differ per firmware
                continue
        try:
            c.read_sizes()
            info.user_count, info.template_count = c.users, c.fingers
            info.punch_count, info.punch_capacity = c.records, getattr(c, "rec_cap", None)
        except Exception:  # noqa: S110
            pass
        with contextlib.suppress(Exception):
            info.device_time = c.get_time()
        return info

    def get_users(self) -> list[PulledUser]:
        users = []
        for u in self.conn.get_users():
            self._uids[str(u.user_id)] = u.uid
            users.append(PulledUser(pin=str(u.user_id), name=u.name, privilege=u.privilege,
                                    uid=u.uid))
        return users

    def get_templates(self) -> list[PulledTemplate]:
        if not self._uids:
            self.get_users()
        by_uid = {uid: pin for pin, uid in self._uids.items()}
        with self._locked():
            fingers = self.conn.get_templates()
        return [PulledTemplate(pin=by_uid.get(f.uid, str(f.uid)), finger_index=int(f.fid),
                               template=bytes(f.template), valid=bool(f.valid),
                               algorithm_version=self.algorithm_version)
                for f in fingers if f.template]

    def get_attendance(self) -> list[PulledPunch]:
        with self._locked():
            records = self.conn.get_attendance()
        return [PulledPunch(pin=str(r.user_id), local_time=r.timestamp, status=str(r.punch),
                            verify=int(r.status) if r.status is not None else None,
                            raw=f"{r.user_id}\t{r.timestamp}\t{r.punch}\t{r.status}")
                for r in records]

    def _uid_for(self, pin: str) -> int:
        if not self._uids:
            self.get_users()
        if pin in self._uids:
            return self._uids[pin]
        uid = max(self._uids.values(), default=0) + 1
        self._uids[pin] = uid
        return uid

    def set_user(self, user: PulledUser) -> None:
        uid = self._uid_for(user.pin)
        self.conn.set_user(uid=uid, name=user.name[:24], privilege=user.privilege,
                           password="", group_id="", user_id=user.pin, card=0)

    def set_templates(self, user: PulledUser, templates: list[PulledTemplate]) -> None:
        from zk.finger import Finger
        from zk.user import User

        uid = self._uid_for(user.pin)
        zk_user = User(uid, user.name[:24], user.privilege, "", "", user.pin, 0)
        fingers = [Finger(uid, t.finger_index, 1 if t.valid else 0, t.template)
                   for t in templates]
        with self._locked():
            self.conn.save_user_template(zk_user, fingers)

    def delete_user(self, pin: str) -> None:
        if not self._uids:
            self.get_users()
        uid = self._uids.get(pin)
        if uid is not None:
            self.conn.delete_user(uid=uid)
            self._uids.pop(pin, None)

    def set_time(self, local_time: datetime) -> None:
        self.conn.set_time(local_time)

    def live_capture(self, timeout: int = 10) -> Iterator[PulledPunch | None]:
        for record in self.conn.live_capture(new_timeout=timeout):
            if record is None:
                yield None
                continue
            yield PulledPunch(pin=str(record.user_id), local_time=record.timestamp,
                              status=str(record.punch),
                              verify=int(record.status) if record.status is not None else None,
                              raw=f"{record.user_id}\t{record.timestamp}\t{record.punch}")
