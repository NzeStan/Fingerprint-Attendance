from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["fingerprint_attendance.tasks"], related_name="celery")

from fingerprint_attendance.tasks.celery import beat_schedule  # noqa: E402

app.conf.beat_schedule = beat_schedule()
