"""Hook registry: ``HOOKS = {"punch_received": ["myapp.hooks.on_punch"]}``.

A hook is any callable ``hook(event: str, payload: dict)``. The key ``"*"`` subscribes to every
event. Hooks run through the task backend when ``HOOKS_ASYNC`` is on, and a failing hook is
logged and swallowed so it can never break ingestion.
"""

from __future__ import annotations

from typing import Any

from .conf import import_object, settings
from .utils.logging import get_logger

logger = get_logger(__name__)

_runtime_hooks: dict[str, list[Any]] = {}


def register_hook(event: str, func: Any) -> None:
    """Register a hook in code (in addition to the ``HOOKS`` setting)."""
    _runtime_hooks.setdefault(event, []).append(func)


def unregister_hook(event: str, func: Any) -> None:
    if func in _runtime_hooks.get(event, []):
        _runtime_hooks[event].remove(func)


def hooks_for(event: str) -> list[Any]:
    configured = settings.HOOKS or {}
    found: list[Any] = []
    for key in (event, "*"):
        paths = configured.get(key, [])
        if isinstance(paths, str):
            paths = [paths]
        found.extend(paths)
        found.extend(_runtime_hooks.get(key, []))
    return found


def run_hooks(event: str, payload: dict[str, Any]) -> None:
    from .tasks.backends import enqueue

    for hook in hooks_for(event):
        if settings.HOOKS_ASYNC and isinstance(hook, str):
            enqueue("fingerprint_attendance.hooks.call_hook", hook, event, payload)
        else:
            call_hook(hook, event, payload)


def call_hook(hook: Any, event: str, payload: dict[str, Any]) -> None:
    try:
        func = import_object(hook, setting="HOOKS")
        func(event, payload)
    except Exception:
        logger.exception("Hook %r failed for event %s", hook, event)
