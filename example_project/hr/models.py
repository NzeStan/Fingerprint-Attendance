"""The project's own employee model (EMPLOYEE_MODEL = "hr.Employee")."""

from __future__ import annotations

from django.db import models


class Employee(models.Model):
    staff_id = models.CharField(max_length=20, unique=True)
    badge_number = models.CharField(max_length=9, unique=True, help_text="Used as device PIN")
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    department = models.CharField(max_length=100, blank=True)
    overtime_eligible = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}"


class WebhookEvent(models.Model):
    """What our sample webhook receiver stored."""

    delivery_id = models.CharField(max_length=64, unique=True)
    event = models.CharField(max_length=64)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)
