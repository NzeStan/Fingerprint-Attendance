"""Task backends: ``enqueue(func_path, *args, **kwargs)``.

Arguments must be JSON-serializable (pass primary keys, not model instances) so the same call
works inline, through Celery, or through Django's task framework.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from ..conf import import_object, settings
from ..utils.logging import get_logger

logger = get_logger(__name__)


def run_task(func_path: str, args: list[Any] | tuple[Any, ...] = (),
             kwargs: dict[str, Any] | None = None) -> Any:
    """Execute a task by dotted path (the single entry point every backend calls)."""
    func = import_object(func_path)
    return func(*args, **(kwargs or {}))


class BaseTaskBackend:
    def __init__(self, **options: Any) -> None:
        self.options = options

    def enqueue(self, func_path: str, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class SyncTaskBackend(BaseTaskBackend):
    """Runs tasks inline. Errors are logged and swallowed unless ``raise_errors`` is set in
    ``TASK_BACKEND_OPTIONS`` (useful in tests)."""

    def enqueue(self, func_path: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return run_task(func_path, args, kwargs)
        except Exception:
            if self.options.get("raise_errors"):
                raise
            logger.exception("Inline task %s failed", func_path)
            return None


class CeleryTaskBackend(BaseTaskBackend):
    """Sends tasks to Celery after the current transaction commits."""

    def enqueue(self, func_path: str, *args: Any, **kwargs: Any) -> Any:
        from .celery import run as celery_run

        options: dict[str, Any] = {}
        if self.options.get("queue"):
            options["queue"] = self.options["queue"]

        def send() -> None:
            celery_run.apply_async(args=[func_path, list(args), kwargs], **options)

        transaction.on_commit(send, robust=True)


class DjangoTaskBackend(BaseTaskBackend):
    """Uses Django's built-in task framework (Django 6.0+)."""

    def enqueue(self, func_path: str, *args: Any, **kwargs: Any) -> Any:
        from .django_tasks import run as django_run

        task = django_run
        using: dict[str, Any] = {}
        if self.options.get("queue_name"):
            using["queue_name"] = self.options["queue_name"]
        if self.options.get("backend"):
            using["backend"] = self.options["backend"]
        if using:
            task = task.using(**using)

        def send() -> None:
            task.enqueue(func_path, list(args), kwargs)

        transaction.on_commit(send, robust=True)


def get_task_backend() -> BaseTaskBackend:
    backend: Any = settings.import_("TASK_BACKEND")
    options = settings.TASK_BACKEND_OPTIONS or {}
    if isinstance(backend, type):
        return backend(**options)
    return backend


def enqueue(func_path: str, *args: Any, **kwargs: Any) -> Any:
    return get_task_backend().enqueue(func_path, *args, **kwargs)
