"""Celery integration (``[celery]`` extra).

Add the periodic jobs to your beat schedule::

    from fingerprint_attendance.tasks.celery import beat_schedule
    app.conf.beat_schedule = {**app.conf.beat_schedule, **beat_schedule()}
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from celery import shared_task

from .backends import run_task


@shared_task(name="fingerprint_attendance.run", ignore_result=True)
def run(func_path: str, args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None) -> Any:
    return run_task(func_path, args or [], kwargs or {})


#: (job dotted path, default interval)
PERIODIC_JOBS: dict[str, timedelta] = {
    "fingerprint_attendance.tasks.jobs.check_devices": timedelta(minutes=1),
    "fingerprint_attendance.tasks.jobs.expire_commands": timedelta(minutes=5),
    "fingerprint_attendance.tasks.jobs.retry_commands": timedelta(minutes=1),
    "fingerprint_attendance.tasks.jobs.expire_enrollment_sessions": timedelta(minutes=1),
    "fingerprint_attendance.tasks.jobs.retry_webhook_deliveries": timedelta(minutes=1),
    "fingerprint_attendance.tasks.jobs.purge_retention": timedelta(hours=24),
    "fingerprint_attendance.tasks.jobs.generate_absences": timedelta(hours=1),
}


def beat_schedule(overrides: dict[str, timedelta | None] | None = None,
                  queue: str | None = None) -> dict[str, dict[str, Any]]:
    """Celery beat entries. Pass ``{job_path: None}`` in ``overrides`` to drop a job."""
    schedule: dict[str, dict[str, Any]] = {}
    for path, interval in {**PERIODIC_JOBS, **(overrides or {})}.items():
        if interval is None:
            continue
        entry: dict[str, Any] = {
            "task": "fingerprint_attendance.run",
            "schedule": interval,
            "args": [path, [], {}],
        }
        if queue:
            entry["options"] = {"queue": queue}
        schedule[f"fpa:{path.rsplit('.', 1)[-1]}"] = entry
    return schedule
