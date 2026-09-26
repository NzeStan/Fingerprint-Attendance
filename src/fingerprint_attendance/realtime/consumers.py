"""A ready WebSocket consumer (``[channels]`` extra).

Clients connect and receive every broadcast event; they may narrow subscriptions by sending
``{"subscribe": ["fpa.device.ABC123"]}`` (group names produced by
``REALTIME_GROUP_NAME_BUILDER``). Access is decided by ``REALTIME_CONSUMER_PERMISSION``.
"""

from __future__ import annotations

from typing import Any

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from ..conf import settings
from .backends import ALL_GROUP, staff_only

__all__ = ["EventsConsumer", "staff_only"]


class EventsConsumer(AsyncJsonWebsocketConsumer):
    default_groups = [ALL_GROUP]

    async def connect(self) -> None:
        check = settings.import_("REALTIME_CONSUMER_PERMISSION")
        if not check(self.scope):
            await self.close(code=4403)
            return
        self.joined: set[str] = set()
        for group in self.default_groups:
            await self._join(group)
        await self.accept()

    async def _join(self, group: str) -> None:
        await self.channel_layer.group_add(group, self.channel_name)
        self.joined.add(group)

    async def disconnect(self, code: int) -> None:
        for group in getattr(self, "joined", set()):
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        groups = content.get("subscribe") if isinstance(content, dict) else None
        if not isinstance(groups, list):
            return
        # narrowing: leave the catch-all group and join only the requested ones
        for group in list(self.joined):
            await self.channel_layer.group_discard(group, self.channel_name)
        self.joined.clear()
        for group in groups[:50]:
            if isinstance(group, str) and group.startswith("fpa."):
                await self._join(group)
        await self.send_json({"subscribed": sorted(self.joined)})

    async def fpa_event(self, message: dict[str, Any]) -> None:
        await self.send_json({"event": message["event"], "payload": message["payload"]})
