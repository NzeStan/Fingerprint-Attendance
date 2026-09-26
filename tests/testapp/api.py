"""Consumer-style API overrides used by the tests."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers
from rest_framework.permissions import BasePermission

from fingerprint_attendance.api.serializers import DeviceSerializer
from fingerprint_attendance.api.views import DeviceViewSet


class BrandedDeviceSerializer(DeviceSerializer):
    brand = serializers.SerializerMethodField()

    class Meta(DeviceSerializer.Meta):
        fields = [*DeviceSerializer.Meta.fields, "brand"]

    def get_brand(self, obj: Any) -> str:
        return "ACME"


class ReadOnlyDeviceViewSet(DeviceViewSet):
    http_method_names = ["get", "head", "options"]


class AllowAll(BasePermission):
    def has_permission(self, request: Any, view: Any) -> bool:
        return True


class DenyAll(BasePermission):
    def has_permission(self, request: Any, view: Any) -> bool:
        return False
