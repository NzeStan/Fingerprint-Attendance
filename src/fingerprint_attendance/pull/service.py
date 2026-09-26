"""Pull-mode operations. Punches go through the same ingestion pipeline as ADMS."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from ..conf import settings
from ..constants import ConnectionState
from ..ingestion.candidates import PunchCandidate
from ..ingestion.pipeline import IngestResult, ingest_punches
from ..utils.logging import get_logger
from ..utils.timeutils import device_zone, now, to_utc
from .base import BasePullAdapter, PulledPunch, PullError, adapter_for

logger = get_logger(__name__)


def _candidates(device: Any, punches: list[PulledPunch]) -> list[PunchCandidate]:
    return [PunchCandidate(pin=p.pin, local_time=p.local_time, raw_state=p.status,
                           verify_mode=p.verify, work_code=p.work_code, raw_payload=p.raw,
                           source="pull", device=device) for p in punches]


def _record_info(device: Any, conn: BasePullAdapter) -> None:
    from ..services.devices import check_log_capacity, record_clock_drift

    try:
        info = conn.get_info()
    except Exception:
        logger.warning("could not read info from %s", device.serial_number, exc_info=True)
        return
    for field, value in (("firmware_version", info.firmware_version),
                         ("platform", info.platform),
                         ("fp_algorithm_version", info.fp_algorithm_version),
                         ("reported_user_count", info.user_count),
                         ("reported_template_count", info.template_count),
                         ("reported_punch_count", info.punch_count),
                         ("log_capacity", info.punch_capacity)):
        if value not in (None, ""):
            setattr(device, field, value)
    device.last_seen_at = now()
    device.connection_state = ConnectionState.ONLINE
    device.save()
    check_log_capacity(device)
    if info.device_time is not None:
        record_clock_drift(device, info.device_time, source="pull")


def import_attendance(device: Any, *, full: bool = False,
                      adapter: BasePullAdapter | None = None) -> IngestResult:
    """Import punches newer than the device's ``pull_last_record_at`` marker (all with
    ``full=True``; dedupe prevents duplicates)."""
    conn = adapter or adapter_for(device)
    with conn:
        _record_info(device, conn)
        punches = conn.get_attendance()
    marker = device.pull_last_record_at
    zone = device_zone(device)
    if marker is not None and not full:
        punches = [p for p in punches if to_utc(p.local_time, zone) > marker]
    result = ingest_punches(_candidates(device, punches), device=device)
    if punches:
        newest = max(to_utc(p.local_time, zone) for p in punches)
        if marker is None or newest > marker:
            type(device).objects.filter(pk=device.pk).update(pull_last_record_at=newest)
            device.pull_last_record_at = newest
    logger.info("pull import %s: %d read, %d new", device.serial_number, len(punches),
                result.created_count)
    return result


def sync_device(device: Any, *, adapter: BasePullAdapter | None = None,
                remove_extra: bool = False) -> dict[str, int]:
    """Push missing users/templates (in scope) to a pull-mode device; optionally delete users
    the server does not know."""
    from ..models import Enrollee
    from ..services.enrollees import device_name_for
    from ..sync.engine import choose_templates, mark_present
    from ..sync.strategies import get_sync_strategy
    from .base import PulledTemplate, PulledUser

    summary = {"users": 0, "templates": 0, "deleted": 0}
    conn = adapter or adapter_for(device)
    with conn:
        _record_info(device, conn)
        on_device = {u.pin for u in conn.get_users()}
        device_templates = {(t.pin, t.finger_index) for t in conn.get_templates()}
        in_scope = list(get_sync_strategy().enrollees_for(device).prefetch_related("templates"))
        for enrollee in in_scope:
            templates = choose_templates(device, list(enrollee.templates.all()))
            user = PulledUser(pin=enrollee.device_pin, name=device_name_for(enrollee),
                              privilege=int(enrollee.privilege))
            if enrollee.device_pin not in on_device:
                conn.set_user(user)
                summary["users"] += 1
            missing = [t for t in templates
                       if (enrollee.device_pin, t.finger_index) not in device_templates]
            if missing:
                conn.set_templates(user, [PulledTemplate(pin=enrollee.device_pin,
                                                         finger_index=t.finger_index,
                                                         template=t.get_data())
                                          for t in templates])
                summary["templates"] += len(missing)
            for t in templates:
                mark_present(device, enrollee, template=t)
        if remove_extra:
            known = set(Enrollee.objects.filter(is_active=True).values_list("device_pin",
                                                                            flat=True))
            for pin in on_device - known:
                conn.delete_user(pin)
                summary["deleted"] += 1
    return summary


class LiveCapture:
    """Long-running live capture with reconnect/backoff and graceful shutdown.

    On every (re)connect it first gap-fills from the ``pull_last_record_at`` marker so punches
    made while disconnected are not missed, then resumes live capture.
    """

    def __init__(self, device: Any, *, adapter_factory: Callable[[Any], BasePullAdapter] | None
                 = None, timeout: int = 10, sleep: Callable[[float], None] = time.sleep) -> None:
        self.device = device
        self.adapter_factory = adapter_factory or adapter_for
        self.timeout = timeout
        self.sleep = sleep
        self.stop_event = threading.Event()

    def stop(self, *args: Any) -> None:
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)

    def run(self, max_cycles: int | None = None) -> None:
        backoff = 1.0
        cycles = 0
        max_backoff = settings.PULL_RECONNECT_BACKOFF_MAX.total_seconds()
        while not self.stop_event.is_set():
            cycles += 1
            if max_cycles is not None and cycles > max_cycles:
                return
            try:
                import_attendance(self.device, adapter=self.adapter_factory(self.device))
                self._capture()
                backoff = 1.0
            except PullError as exc:
                logger.warning("live capture on %s: %s; retrying in %.0fs",
                               self.device.serial_number, exc, backoff)
                type(self.device).objects.filter(pk=self.device.pk).update(
                    connection_state=ConnectionState.OFFLINE)
                if not self.stop_event.is_set():
                    self.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    def _capture(self) -> None:
        conn = self.adapter_factory(self.device)
        with conn:
            for punch in conn.live_capture(timeout=self.timeout):
                if self.stop_event.is_set():
                    return
                if punch is None:
                    continue
                result = ingest_punches(_candidates(self.device, [punch]), device=self.device)
                if result.created:
                    marker = result.created[-1].punched_at
                    type(self.device).objects.filter(pk=self.device.pk).update(
                        pull_last_record_at=marker, last_seen_at=now())


def poll_forever(devices: list[Any], *, interval: timedelta | None = None,
                 stop_event: threading.Event | None = None, max_rounds: int | None = None) -> None:
    stop_event = stop_event or threading.Event()
    wait = (interval or settings.PULL_POLL_INTERVAL).total_seconds()
    rounds = 0
    while not stop_event.is_set():
        rounds += 1
        for device in devices:
            try:
                import_attendance(device)
            except PullError as exc:
                logger.warning("poll %s failed: %s", device.serial_number, exc)
        if max_rounds is not None and rounds >= max_rounds:
            return
        stop_event.wait(wait)
