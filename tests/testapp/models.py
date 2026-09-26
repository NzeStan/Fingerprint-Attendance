"""A custom employee model, proving EMPLOYEE_MODEL can point anywhere."""

from __future__ import annotations

from django.db import models


class Employee(models.Model):
    staff_number = models.CharField(max_length=20, unique=True)
    full_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)

    def __str__(self) -> str:
        return self.full_name
