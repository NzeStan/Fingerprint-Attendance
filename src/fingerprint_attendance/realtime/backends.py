"""Realtime broadcast backends (``REALTIME_BACKEND``)."""

from __future__ import annotations

from typing import Any

from ..conf import settings
from ..utils.logging import get_logger

logger = get_logger(__name__)

ALL_GROUP = "fpa.all"


def default_group_names(event: str, payload: dict[str, Any]) -> list[str]:
    """Everything goes to ``fpa.all``; device events also go to ``fpa.device.<serial>``."""
    groups = [ALL_GROUP, f"fpa.event.{event}"]
    serial = payload.get("device_serial") or payload.get("serial_number") or (
        (payload.get("device") or {}).get("serial_number") if isinstance(payload.get("device"),
                                                                       dict) else None)
    if serial:
        groups.append(f"fpa.device.{_safe(serial)}")
    return groups


def staff_only(scope: dict[str, Any]) -> bool:
    """Default ``REALTIME_CONSUMER_PERMISSION``: authenticated staff users only."""
    user = scope.get("user")
    return bool(user is not None and getattr(user, "is_authenticated", False)
                and getattr(user, "is_staff", False))


def _safe(value: str) -> str:
    # channel group names: ASCII alphanumerics, hyphens, underscores, periods; < 100 chars
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value))[:80]


class BaseRealtimeBackend:
    def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    def group_names(self, event: str, payload: dict[str, Any]) -> list[str]:
        builder = settings.import_("REALTIME_GROUP_NAME_BUILDER")
        return list(builder(event, payload))


class NullRealtimeBackend(BaseRealtimeBackend):
    def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        return None


class ChannelsRealtimeBackend(BaseRealtimeBackend):
    """Sends ``{"type": "fpa.event", "event": ..., "payload": ...}`` to each group via the
    channel layer. Pair with :class:`fingerprint_attendance.realtime.consumers.EventsConsumer`."""

    def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            logger.warning("REALTIME_BACKEND is channels but no CHANNEL_LAYERS is configured")
            return
        message = {"type": "fpa.event", "event": event, "payload": payload}
        for group in self.group_names(event, payload):
            async_to_sync(layer.group_send)(group, message)


def get_realtime_backend() -> BaseRealtimeBackend:
    obj = settings.import_("REALTIME_BACKEND")
    return obj() if isinstance(obj, type) else obj
