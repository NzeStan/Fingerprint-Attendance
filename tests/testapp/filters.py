"""A consumer FilterSet swapped in with FILTERSET_OVERRIDES."""

from __future__ import annotations

import django_filters

from fingerprint_attendance.models import Enrollee


class PinPrefixFilterSet(django_filters.FilterSet):
    pin_prefix = django_filters.CharFilter(field_name="device_pin", lookup_expr="startswith")

    class Meta:
        model = Enrollee
        fields: list[str] = []
