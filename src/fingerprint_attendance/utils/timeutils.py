"""Timezone helpers. Everything is stored in UTC; devices speak naive local time."""

from __future__ import annotations

import functools
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from django.utils import timezone as dj_tz

if TYPE_CHECKING:
    from ..models import Device

UTC = timezone.utc

_DEVICE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y%m%d%H%M%S",
    "%d/%m/%Y %H:%M:%S",
)


@functools.lru_cache(maxsize=256)
def get_zone(name: str) -> ZoneInfo:
    """Return a ``ZoneInfo``; raises ``ZoneInfoNotFoundError``/``ValueError`` if invalid."""
    return ZoneInfo(name)


def default_device_zone() -> ZoneInfo:
    from ..conf import settings

    return get_zone(settings.DEFAULT_DEVICE_TIMEZONE or "UTC")


def device_zone(device: Device | None) -> ZoneInfo:
    if device is not None and device.timezone:
        try:
            return get_zone(device.timezone)
        except Exception:  # an invalid per-device value must not break ingestion
            pass
    return default_device_zone()


def now() -> datetime:
    return dj_tz.now()


def parse_device_datetime(value: str) -> datetime:
    """Parse a naive device timestamp in any of the formats firmware is known to send."""
    text = value.strip()
    for fmt in _DEVICE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised device time {value!r}")


def localize(naive: datetime, zone: ZoneInfo) -> datetime:
    """Attach ``zone`` to a naive datetime. Ambiguous DST times resolve to the first occurrence;
    non-existent times (spring-forward gap) are shifted forward by zoneinfo semantics."""
    if naive.tzinfo is not None:
        return naive
    return naive.replace(tzinfo=zone, fold=0)


def to_utc(naive_or_aware: datetime, zone: ZoneInfo) -> datetime:
    return localize(naive_or_aware, zone).astimezone(UTC)


def to_local(aware: datetime, zone: ZoneInfo) -> datetime:
    return aware.astimezone(zone)


def utc_offset_hours(zone: ZoneInfo, at: datetime | None = None) -> float:
    at = at or now()
    offset = at.astimezone(zone).utcoffset() or timedelta(0)
    return offset.total_seconds() / 3600


def format_offset(zone: ZoneInfo, at: datetime | None = None) -> str:
    """``+0100`` style offset."""
    at = at or now()
    offset = at.astimezone(zone).utcoffset() or timedelta(0)
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 60:02d}{total % 60:02d}"


def zk_encode_time(dt: datetime) -> int:
    """Encode a naive local datetime in ZKTeco's compact integer format.

    ``((year-2000)*12*31 + (month-1)*31 + day-1) * 86400 + (hour*60 + minute)*60 + second``.
    Used by ``rtdata?type=rttime`` and some ``SET OPTION DateTime`` firmware.
    """
    days = ((dt.year - 2000) * 12 * 31) + ((dt.month - 1) * 31) + (dt.day - 1)
    return days * 86400 + (dt.hour * 60 + dt.minute) * 60 + dt.second


def zk_decode_time(value: int) -> datetime:
    second = value % 60
    value //= 60
    minute = value % 60
    value //= 60
    hour = value % 24
    value //= 24
    day = value % 31 + 1
    value //= 31
    month = value % 12 + 1
    value //= 12
    return datetime(value + 2000, month, day, hour, minute, second)


def daterange(start: date, end: date) -> list[date]:
    """Inclusive list of dates."""
    if end < start:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def ensure_aware(value: Any, zone: ZoneInfo | None = None) -> datetime:
    if isinstance(value, str):
        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(value)
        if parsed is None:
            raise ValueError(f"invalid datetime {value!r}")
        value = parsed
    if not isinstance(value, datetime):
        raise ValueError(f"invalid datetime {value!r}")
    if value.tzinfo is None:
        value = localize(value, zone or default_device_zone())
    return value
