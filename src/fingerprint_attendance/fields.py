"""Custom model fields."""

from __future__ import annotations

from typing import Any

from django.db import models

from .conf import employee_model_label


class EmployeeOneToOneField(models.OneToOneField):
    """One-to-one to the configured ``EMPLOYEE_MODEL``.

    The target is resolved from settings every time the field is constructed and is left out
    of :meth:`deconstruct`, so the package's migrations never change when a project points
    ``EMPLOYEE_MODEL`` at its own model (the same idea as ``AUTH_USER_MODEL``). As with
    ``AUTH_USER_MODEL``, choose the model before running the first migration.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.pop("to", None)
        kwargs.setdefault("on_delete", models.CASCADE)
        super().__init__(employee_model_label(), *args, **kwargs)

    def deconstruct(self) -> Any:
        name, _path, args, kwargs = super().deconstruct()
        kwargs.pop("to", None)
        return name, "fingerprint_attendance.fields.EmployeeOneToOneField", args, kwargs
