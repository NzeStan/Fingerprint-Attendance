"""Hooks, strategies and callables referenced from test settings."""

from __future__ import annotations

from typing import Any

CALLS: list[tuple[str, dict[str, Any]]] = []


def shout_name(employee: Any) -> str:
    return str(employee.full_name).upper()


def record(event: str, payload: dict[str, Any]) -> None:
    CALLS.append((event, payload))


def explode(event: str, payload: dict[str, Any]) -> None:
    raise RuntimeError("hook failure must not break ingestion")


def only_even_devices(enrollee: Any) -> Any:
    """A custom SYNC_STRATEGY callable."""
    from fingerprint_attendance.models import Device

    return Device.objects.filter(status="active", serial_number__endswith="2")


def custom_dedupe_key(candidate: Any) -> str:
    """Ignore the device: the same person at the same second is one punch."""
    return f"{candidate.pin}|{candidate.punched_at:%Y%m%d%H%M%S}"
