"""Single dispatch point for package events.

``emit()`` fans an event out to, in order:

1. the Django signal of the same name (``send_robust``: receiver errors are logged, never
   propagated into ingestion),
2. configured ``HOOKS`` (through the task backend when ``HOOKS_ASYNC``),
3. outbound webhooks (when ``WEBHOOKS_ENABLED``),
4. the realtime backend (for events listed in ``REALTIME_EVENTS``).

With ``EVENTS_ON_COMMIT`` (default) dispatch happens after the surrounding transaction commits,
so nobody is notified about data that is later rolled back.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.dispatch import Signal

from . import signals
from .conf import settings
from .utils.logging import get_logger

logger = get_logger(__name__)

#: event name -> signal. Consumers may register additional events with :func:`register_event`.
EVENTS: dict[str, Signal] = {
    name: obj for name, obj in vars(signals).items() if isinstance(obj, Signal)
}


def after_commit(func: Any) -> None:
    """Run ``func`` after the current transaction commits (immediately when
    ``EVENTS_ON_COMMIT`` is off or no transaction is active)."""
    if settings.EVENTS_ON_COMMIT:
        transaction.on_commit(func, robust=True)
    else:
        func()


def register_event(name: str, signal: Signal | None = None) -> Signal:
    sig = signal or Signal()
    EVENTS[name] = sig
    return sig


def emit(event: str, payload: dict[str, Any] | None = None, *, sender: Any = None,
         **signal_kwargs: Any) -> None:
    if event not in EVENTS:
        raise KeyError(f"Unknown event {event!r}; register it with register_event()")
    body = dict(payload or {})

    def dispatch() -> None:
        _dispatch(event, body, sender=sender, signal_kwargs=signal_kwargs)

    if settings.EVENTS_ON_COMMIT:
        transaction.on_commit(dispatch, robust=True)
    else:
        dispatch()


def _dispatch(event: str, payload: dict[str, Any], *, sender: Any,
              signal_kwargs: dict[str, Any]) -> None:
    signal = EVENTS[event]
    for receiver, result in signal.send_robust(sender=sender or event, payload=payload,
                                               event=event, **signal_kwargs):
        if isinstance(result, Exception):
            logger.error("Receiver %r for %s failed: %r", receiver, event, result,
                         exc_info=(type(result), result, result.__traceback__))
    for step in (_run_hooks, _queue_webhooks, _broadcast):
        try:
            step(event, payload)
        except Exception:  # isolation: one failing integration never breaks the caller
            logger.exception("Event integration %s failed for %s", step.__name__, event)


def _run_hooks(event: str, payload: dict[str, Any]) -> None:
    from .hooks import run_hooks

    run_hooks(event, payload)


def _queue_webhooks(event: str, payload: dict[str, Any]) -> None:
    if not settings.WEBHOOKS_ENABLED:
        return
    from .webhooks.delivery import queue_event

    queue_event(event, payload)


def _broadcast(event: str, payload: dict[str, Any]) -> None:
    if event not in (settings.REALTIME_EVENTS or []):
        return
    from .realtime.backends import get_realtime_backend

    get_realtime_backend().broadcast(event, payload)
