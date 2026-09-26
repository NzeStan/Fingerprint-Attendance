"""Sync strategies decide which devices must hold an enrollee.

Set ``SYNC_STRATEGY`` to ``"all"``, ``"groups"``, a :class:`BaseSyncStrategy` subclass path, or
a plain callable ``(enrollee) -> QuerySet[Device]``.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from ..conf import settings
from ..constants import DeviceStatus


def _active_devices() -> QuerySet:
    from ..models import Device

    return Device.objects.filter(status=DeviceStatus.ACTIVE)


class BaseSyncStrategy:
    def devices_for(self, enrollee: Any) -> QuerySet:
        raise NotImplementedError

    def enrollees_for(self, device: Any) -> QuerySet:
        """Active enrollees in scope for ``device`` (used for backfills). The generic
        implementation evaluates :meth:`devices_for` per enrollee; override for speed."""
        from ..models import Enrollee

        ids = [e.pk for e in Enrollee.objects.active()
               if self.devices_for(e).filter(pk=device.pk).exists()]
        return Enrollee.objects.filter(pk__in=ids)


class AllDevicesStrategy(BaseSyncStrategy):
    """Every active enrollee on every active device."""

    def devices_for(self, enrollee: Any) -> QuerySet:
        return _active_devices()

    def enrollees_for(self, device: Any) -> QuerySet:
        from ..models import Enrollee

        return Enrollee.objects.active()


class DeviceGroupStrategy(BaseSyncStrategy):
    """Enrollees go to devices sharing at least one device group (sites/branches).
    Enrollees without groups go everywhere only when ``SYNC_UNGROUPED_TO_ALL`` is on."""

    def devices_for(self, enrollee: Any) -> QuerySet:
        group_ids = list(enrollee.groups.values_list("pk", flat=True))
        if not group_ids:
            return _active_devices() if settings.SYNC_UNGROUPED_TO_ALL else _active_devices().none()
        return _active_devices().filter(groups__in=group_ids).distinct()

    def enrollees_for(self, device: Any) -> QuerySet:
        from django.db.models import Q

        from ..models import Enrollee

        group_ids = list(device.groups.values_list("pk", flat=True))
        q = Q(groups__in=group_ids) if group_ids else Q(pk__in=[])
        if settings.SYNC_UNGROUPED_TO_ALL:
            q |= Q(groups__isnull=True)
        return Enrollee.objects.active().filter(q).distinct()


class CallableStrategy(BaseSyncStrategy):
    def __init__(self, func: Any) -> None:
        self.func = func

    def devices_for(self, enrollee: Any) -> QuerySet:
        return self.func(enrollee)


def get_sync_strategy() -> BaseSyncStrategy:
    obj = settings.import_("SYNC_STRATEGY")
    if isinstance(obj, type):
        return obj()
    if isinstance(obj, BaseSyncStrategy):
        return obj
    return CallableStrategy(obj)
