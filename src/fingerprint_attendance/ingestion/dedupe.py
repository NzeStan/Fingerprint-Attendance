"""Exact-duplicate keys. The same physical punch always produces the same key, whether it
arrives by ADMS, pull mode or import, so re-sends and cursor resets never create duplicates."""

from __future__ import annotations

import hashlib

from .candidates import PunchCandidate


def default_dedupe_key(candidate: PunchCandidate) -> str:
    serial = candidate.device.serial_number if candidate.device is not None else "-"
    assert candidate.punched_at is not None
    stamp = candidate.punched_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{serial}|{candidate.pin}|{stamp}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
