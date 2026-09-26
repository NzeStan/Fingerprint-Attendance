"""Punch state resolvers (check-in / check-out).

A resolver receives the batch of new, non-duplicate candidates (sorted by time) and sets
``candidate.state``. ``context.previous_punch_count(candidate)`` gives the number of punches
already stored for the same person on the same work date, for sequence-based resolvers.

Note: sequence-based resolvers assign states at ingestion time. When a backlog inserts older
punches between existing ones, stored states are not rewritten (punches are immutable); the
default attendance processor derives first-in/last-out from times, not states.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from ..conf import settings
from .candidates import PunchCandidate


class ResolverContext:
    def __init__(self, existing_counts: dict[tuple[str, date], int]) -> None:
        self._counts = existing_counts

    def previous_punch_count(self, candidate: PunchCandidate) -> int:
        assert candidate.work_date is not None
        return self._counts.get((candidate.pin, candidate.work_date), 0)


class BasePunchStateResolver:
    #: whether the pipeline must compute ``previous_punch_count`` (costs one query)
    needs_history = False

    def resolve(self, candidates: list[PunchCandidate], context: ResolverContext) -> None:
        raise NotImplementedError


class TrustDeviceResolver(BasePunchStateResolver):
    """Maps the device's status code through ``PUNCH_STATE_MAP``."""

    def resolve(self, candidates: list[PunchCandidate], context: ResolverContext) -> None:
        mapping = {str(k): v for k, v in (settings.PUNCH_STATE_MAP or {}).items()}
        for c in candidates:
            c.state = mapping.get(str(c.raw_state).strip(), "unknown")


class _SequenceResolver(BasePunchStateResolver):
    needs_history = True

    def state_for(self, position: int) -> str:
        raise NotImplementedError

    def resolve(self, candidates: list[PunchCandidate], context: ResolverContext) -> None:
        seen: dict[tuple[str, Any], int] = defaultdict(int)
        for c in candidates:
            key = (c.pin, c.work_date)
            position = context.previous_punch_count(c) + seen[key]
            seen[key] += 1
            c.state = self.state_for(position)


class AlternateResolver(_SequenceResolver):
    """in, out, in, out... per person per work date."""

    def state_for(self, position: int) -> str:
        return "check_in" if position % 2 == 0 else "check_out"


class FirstLastResolver(_SequenceResolver):
    """First punch of the work date is check-in, every later punch is check-out."""

    def state_for(self, position: int) -> str:
        return "check_in" if position == 0 else "check_out"


def get_state_resolver() -> BasePunchStateResolver:
    obj = settings.import_("PUNCH_STATE_RESOLVER")
    return obj() if isinstance(obj, type) else obj
