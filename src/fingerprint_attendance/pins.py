"""Device PIN generation and validation. Set ``PIN_GENERATOR`` to any
``callable(employee) -> str``."""

from __future__ import annotations

from typing import Any

from django.db.models import BigIntegerField, Max
from django.db.models.functions import Cast

from .conf import settings
from .exceptions import InvalidInput


def _max_numeric(model: Any, field: str) -> int:
    value = (model.objects.filter(**{f"{field}__regex": r"^[0-9]+$"})
             .annotate(n=Cast(field, BigIntegerField())).aggregate(m=Max("n"))["m"])
    return int(value or 0)


def sequential_pin(employee: Any = None) -> str:
    """Next number after the highest numeric PIN ever issued (retired PINs included unless
    ``PIN_REUSE_ALLOWED``)."""
    from .models import Enrollee, RetiredPin

    highest = _max_numeric(Enrollee, "device_pin")
    if not settings.PIN_REUSE_ALLOWED:
        highest = max(highest, _max_numeric(RetiredPin, "pin"))
    return str(max(highest + 1, int(settings.PIN_START)))


def employee_field_pin(employee: Any) -> str:
    """Use an employee attribute (``PIN_SOURCE_FIELD`` or ``EMPLOYEE_LOOKUP_FIELD``)."""
    field = settings.PIN_SOURCE_FIELD or settings.EMPLOYEE_LOOKUP_FIELD
    value: Any = employee
    for part in field.split("__"):
        value = getattr(value, part)
    return str(value).strip()


def generate_pin(employee: Any) -> str:
    generator = settings.import_("PIN_GENERATOR")
    return str(generator(employee)).strip()


def validate_pin(pin: str, *, exclude_enrollee: Any = None) -> str:
    from .models import Enrollee, RetiredPin

    pin = str(pin).strip()
    if not pin:
        raise InvalidInput("PIN is empty", details={"field": "device_pin"})
    if len(pin) > settings.PIN_MAX_LENGTH:
        raise InvalidInput(f"PIN longer than {settings.PIN_MAX_LENGTH} characters",
                           details={"field": "device_pin"})
    if settings.PIN_NUMERIC_ONLY and not pin.isdigit():
        raise InvalidInput("PIN must be numeric", details={"field": "device_pin"})
    if any(ch in pin for ch in "\t\r\n= "):
        raise InvalidInput("PIN contains invalid characters", details={"field": "device_pin"})
    clash = Enrollee.objects.filter(device_pin=pin)
    if exclude_enrollee is not None:
        clash = clash.exclude(pk=exclude_enrollee.pk)
    if clash.exists():
        raise InvalidInput("PIN already in use", code="pin_in_use",
                           details={"field": "device_pin"})
    if not settings.PIN_REUSE_ALLOWED and RetiredPin.objects.filter(pin=pin).exists():
        raise InvalidInput("PIN belonged to a deleted enrollee and cannot be reused",
                           code="pin_retired", details={"field": "device_pin"})
    return pin
