"""Django 6 task framework integration (``TASK_BACKEND = "django"``)."""

from __future__ import annotations

from typing import Any

from django.tasks import task

from .backends import run_task


@task()
def run(func_path: str, args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None) -> Any:
    return run_task(func_path, args or [], kwargs or {})
