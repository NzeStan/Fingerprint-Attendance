"""The source-independent punch representation that enters the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class PunchCandidate:
    """A punch before it is stored. Give either ``local_time`` (naive, device-local; converted
    with the device timezone) or ``punched_at`` (aware)."""

    pin: str
    local_time: datetime | None = None
    punched_at: datetime | None = None
    raw_time: str = ""
    raw_state: str = ""
    verify_mode: int | None = None
    work_code: str = ""
    raw_payload: str = ""
    source: str = "adms"
    device: Any = None
    created_by: Any = None
    note: str = ""
    #: optional state forced by the caller (manual punches); skips the resolver
    state: str | None = None
    flags: set[str] = field(default_factory=set)
    # resolved by the pipeline
    enrollee: Any = None
    dedupe_hash: str = ""
    work_date: date | None = None

    @classmethod
    def from_attlog(cls, record: Any, *, device: Any, source: str = "adms") -> PunchCandidate:
        return cls(pin=record.pin, local_time=record.local_time, raw_time=record.raw_time,
                   raw_state=record.status, verify_mode=record.verify,
                   work_code=record.work_code, raw_payload=record.raw, source=source,
                   device=device)
