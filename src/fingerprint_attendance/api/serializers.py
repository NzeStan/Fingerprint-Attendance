"""Serializers. Every one can be replaced via ``SERIALIZER_OVERRIDES``.

Public identifiers are the ``uuid`` columns (exposed as ``id``); template bytes are never
serialized except by :class:`TemplateDataSerializer`, which the API only uses when
``EXPOSE_TEMPLATE_DATA_IN_API`` is on and the caller holds ``view_template_data``.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from rest_framework import serializers

from .. import models
from ..adms.commands import command_registry
from ..conf import get_employee_model, settings
from ..constants import FINGER_NAMES
from ..registry import punch_sources, punch_states


class UUIDRelated(serializers.SlugRelatedField):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("slug_field", "uuid")
        super().__init__(**kwargs)


class DeviceGroupSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    device_count = serializers.IntegerField(read_only=True, required=False)
    enrollee_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = models.DeviceGroup
        fields = ["id", "name", "description", "metadata", "device_count", "enrollee_count",
                  "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class DeviceSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    groups = UUIDRelated(many=True, queryset=models.DeviceGroup.objects.all(), required=False)
    is_online = serializers.BooleanField(read_only=True)
    pull_comm_key = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    status = serializers.CharField(read_only=True)

    class Meta:
        model = models.Device
        fields = [
            "id", "serial_number", "name", "groups", "location", "mode", "status",
            "connection_state", "is_online", "last_seen_at", "last_handshake_at",
            "last_punch_at", "last_ip", "model_name", "firmware_version", "platform",
            "fp_algorithm_version", "push_version", "timezone", "capabilities", "options",
            "reported_user_count", "reported_template_count", "reported_punch_count",
            "reported_face_count", "log_capacity", "clock_drift_seconds", "approved_at",
            "pull_host", "pull_port", "pull_comm_key", "notes", "created_at", "updated_at",
        ]
        read_only_fields = [
            "connection_state", "last_seen_at", "last_handshake_at", "last_punch_at", "last_ip",
            "model_name", "firmware_version", "platform", "push_version", "capabilities",
            "reported_user_count", "reported_template_count", "reported_punch_count",
            "reported_face_count", "clock_drift_seconds", "approved_at", "created_at",
            "updated_at",
        ]

    def validate_timezone(self, value: str) -> str:
        if value:
            from ..utils.timeutils import get_zone

            try:
                get_zone(value)
            except Exception:
                raise serializers.ValidationError("invalid IANA timezone") from None
        return value


class DeviceCreateSerializer(DeviceSerializer):
    approve = serializers.BooleanField(write_only=True, required=False, default=True)

    class Meta(DeviceSerializer.Meta):
        fields = [*DeviceSerializer.Meta.fields, "approve"]


class EmployeeField(serializers.Field):
    """Accepts the ``EMPLOYEE_LOOKUP_FIELD`` value; renders it back."""

    def to_representation(self, value: Any) -> Any:
        field = settings.EMPLOYEE_LOOKUP_FIELD
        result = getattr(value, "pk" if field == "pk" else field)
        return str(result)

    def to_internal_value(self, data: Any) -> Any:
        model = get_employee_model()
        field = settings.EMPLOYEE_LOOKUP_FIELD
        try:
            return model._default_manager.get(**{field: data})
        except (model.DoesNotExist, ValueError, TypeError):
            raise serializers.ValidationError("employee not found") from None


class EnrolleeSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    employee = EmployeeField()
    employee_name = serializers.SerializerMethodField()
    groups = UUIDRelated(many=True, queryset=models.DeviceGroup.objects.all(), required=False)
    device_pin = serializers.CharField(required=False, allow_blank=True, max_length=24)
    display_name = serializers.CharField(required=False, allow_blank=True, max_length=64)
    fingers_enrolled = serializers.SerializerMethodField()

    class Meta:
        model = models.Enrollee
        fields = ["id", "employee", "employee_name", "device_pin", "display_name", "privilege",
                  "groups", "is_active", "consent_state", "deactivated_at", "fingers_enrolled",
                  "metadata", "created_at", "updated_at"]
        read_only_fields = ["is_active", "consent_state", "deactivated_at", "created_at",
                            "updated_at"]

    def get_employee_name(self, obj: Any) -> str:
        from ..services.enrollees import employee_display_name

        return employee_display_name(obj.employee)

    def get_fingers_enrolled(self, obj: Any) -> list[int]:
        prefetched = getattr(obj, "_prefetched_objects_cache", {}).get("templates")
        items = prefetched if prefetched is not None else obj.templates.all()
        return sorted({t.finger_index for t in items})

    def create(self, validated: dict[str, Any]) -> Any:
        from ..services import create_enrollee

        request = self.context.get("request")
        return create_enrollee(
            validated["employee"], device_pin=validated.get("device_pin") or None,
            display_name=validated.get("display_name") or None,
            privilege=validated.get("privilege", 0), groups=validated.get("groups", []),
            metadata=validated.get("metadata"), by=getattr(request, "user", None))

    def update(self, instance: Any, validated: dict[str, Any]) -> Any:
        from ..services import update_enrollee

        request = self.context.get("request")
        validated.pop("employee", None)
        groups = validated.pop("groups", None)
        if "device_pin" in validated and not validated["device_pin"]:
            validated.pop("device_pin")
        return update_enrollee(instance, groups=groups, by=getattr(request, "user", None),
                               **validated)


class ConsentRecordSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    enrollee = UUIDRelated(queryset=models.Enrollee.objects.all())
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = models.ConsentRecord
        fields = ["id", "enrollee", "version", "text_reference", "method", "given_at",
                  "withdrawn_at", "withdrawal_reason", "is_active", "metadata", "created_at"]
        read_only_fields = ["withdrawn_at", "withdrawal_reason", "created_at"]
        extra_kwargs = {"given_at": {"required": False}}

    def create(self, validated: dict[str, Any]) -> Any:
        from ..services import give_consent

        request = self.context.get("request")
        return give_consent(validated["enrollee"], version=validated["version"],
                            text_reference=validated.get("text_reference", ""),
                            method=validated.get("method", ""),
                            given_at=validated.get("given_at"),
                            metadata=validated.get("metadata"),
                            by=getattr(request, "user", None))


class WithdrawConsentSerializer(serializers.Serializer):
    enrollee = UUIDRelated(queryset=models.Enrollee.objects.all())
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class FingerprintTemplateSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    enrollee = UUIDRelated(read_only=True)
    finger_name = serializers.CharField(read_only=True)
    source_device = UUIDRelated(read_only=True)
    source_agent = UUIDRelated(read_only=True)

    class Meta:
        model = models.FingerprintTemplate
        fields = ["id", "enrollee", "finger_index", "finger_name", "algorithm_version", "size",
                  "checksum", "quality", "is_valid", "is_duress", "source", "source_device",
                  "source_agent", "version", "created_at", "updated_at"]
        read_only_fields = fields


class TemplateDataSerializer(FingerprintTemplateSerializer):
    template_data = serializers.SerializerMethodField()

    class Meta(FingerprintTemplateSerializer.Meta):
        fields = [*FingerprintTemplateSerializer.Meta.fields, "template_data"]
        read_only_fields = fields

    def get_template_data(self, obj: Any) -> str:
        from ..services import read_template_data

        request = self.context.get("request")
        return base64.b64encode(read_template_data(obj, by=getattr(request, "user", None),
                                                   purpose="api")).decode("ascii")


class Base64Field(serializers.CharField):
    def to_internal_value(self, data: Any) -> bytes:  # type: ignore[override]
        text = super().to_internal_value(data)
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise serializers.ValidationError("must be base64") from None


class TemplateImportSerializer(serializers.Serializer):
    enrollee = UUIDRelated(queryset=models.Enrollee.objects.all())
    finger_index = serializers.IntegerField(min_value=0, max_value=9)
    algorithm_version = serializers.CharField(max_length=16)
    template = Base64Field()
    quality = serializers.IntegerField(required=False, allow_null=True)


class EnrollmentSessionSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    enrollee = UUIDRelated(queryset=models.Enrollee.objects.all())
    device = UUIDRelated(queryset=models.Device.objects.all(), required=False, allow_null=True)
    agent = UUIDRelated(queryset=models.EnrollmentAgent.objects.all(), required=False,
                        allow_null=True)
    fingers = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=9),
                                    write_only=True, required=False)
    ttl_seconds = serializers.IntegerField(write_only=True, required=False, min_value=30)

    class Meta:
        model = models.EnrollmentSession
        fields = ["id", "enrollee", "device", "agent", "status", "expires_at",
                  "fingers_requested", "fingers_captured", "fingers", "ttl_seconds",
                  "completed_at", "error", "created_at"]
        read_only_fields = ["status", "expires_at", "fingers_requested", "fingers_captured",
                            "completed_at", "error", "created_at"]

    def create(self, validated: dict[str, Any]) -> Any:
        from datetime import timedelta

        from ..services import start_enrollment_session

        request = self.context.get("request")
        ttl = validated.get("ttl_seconds")
        return start_enrollment_session(
            validated["enrollee"], device=validated.get("device"), agent=validated.get("agent"),
            fingers=validated.get("fingers"), by=getattr(request, "user", None),
            ttl=timedelta(seconds=ttl) if ttl else None)


class DeviceCommandSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    command_id = serializers.IntegerField(source="pk", read_only=True)
    device = UUIDRelated(read_only=True)
    enrollee = UUIDRelated(read_only=True)

    class Meta:
        model = models.DeviceCommand
        fields = ["id", "command_id", "device", "command_type", "command_string", "payload",
                  "status", "attempts", "max_attempts", "return_code", "response_body", "error",
                  "sent_at", "completed_at", "expires_at", "next_attempt_at", "correlation_id",
                  "enrollee", "created_at"]
        read_only_fields = fields


class SendCommandSerializer(serializers.Serializer):
    command_type = serializers.ChoiceField(choices=[])
    payload = serializers.DictField(required=False, default=dict)
    correlation_id = serializers.CharField(required=False, allow_blank=True, max_length=64)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["command_type"].choices = command_registry.names()  # type: ignore[attr-defined]


class PunchSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    sequence = serializers.IntegerField(source="pk", read_only=True)
    enrollee = UUIDRelated(read_only=True)
    employee = serializers.SerializerMethodField()
    device = UUIDRelated(read_only=True)
    device_serial = serializers.CharField(source="device.serial_number", read_only=True,
                                          default=None)
    flags = serializers.ListField(source="flag_list", read_only=True)

    class Meta:
        model = models.Punch
        fields = ["id", "sequence", "enrollee", "employee", "raw_pin", "device", "device_serial",
                  "punched_at", "device_local_time", "received_at", "verify_mode", "raw_state",
                  "state", "work_code", "source", "flags", "note", "created_at"]
        read_only_fields = fields

    def get_employee(self, obj: Any) -> Any:
        if not obj.enrollee_id or obj.enrollee is None:
            return None
        return EmployeeField().to_representation(obj.enrollee.employee)


class ManualPunchSerializer(serializers.Serializer):
    enrollee = UUIDRelated(queryset=models.Enrollee.objects.all())
    punched_at = serializers.DateTimeField()
    state = serializers.ChoiceField(choices=[], required=False, allow_blank=True)
    device = UUIDRelated(queryset=models.Device.objects.all(), required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["state"].choices = punch_states.keys()  # type: ignore[attr-defined]


class PunchImportRowSerializer(serializers.Serializer):
    pin = serializers.CharField(max_length=24)
    punched_at = serializers.CharField()
    device_serial = serializers.CharField(required=False, allow_blank=True)
    state = serializers.CharField(required=False, allow_blank=True)
    verify_mode = serializers.IntegerField(required=False, allow_null=True)
    work_code = serializers.CharField(required=False, allow_blank=True)


class PunchImportSerializer(serializers.Serializer):
    rows = PunchImportRowSerializer(many=True)
    source = serializers.ChoiceField(choices=[], default="import")  # type: ignore[assignment]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["source"].choices = punch_sources.keys()  # type: ignore[attr-defined]


class PunchAdjustmentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    punch = UUIDRelated(read_only=True)

    class Meta:
        model = models.PunchAdjustment
        fields = ["id", "punch", "action", "new_state", "new_punched_at", "reason",
                  "created_at"]
        read_only_fields = ["created_at"]


class AttendanceDaySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    enrollee = UUIDRelated(read_only=True)
    worked_seconds = serializers.SerializerMethodField()

    class Meta:
        model = models.AttendanceDay
        fields = ["id", "enrollee", "work_date", "first_in", "last_out", "worked_duration",
                  "worked_seconds", "punch_count", "status", "statuses", "day_type",
                  "day_label", "is_paid", "expected_shift", "computed_at", "processor_version",
                  "extra"]
        read_only_fields = fields

    def get_worked_seconds(self, obj: Any) -> float | None:
        return obj.worked_duration.total_seconds() if obj.worked_duration is not None else None


class RecomputeSerializer(serializers.Serializer):
    start = serializers.DateField()
    end = serializers.DateField()
    enrollees = UUIDRelated(many=True, queryset=models.Enrollee.objects.all(), required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["end"] < attrs["start"]:
            raise serializers.ValidationError("end must not be before start")
        if (attrs["end"] - attrs["start"]).days > 366:
            raise serializers.ValidationError("range is limited to 366 days per request")
        return attrs


class HolidaySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    device_group = UUIDRelated(queryset=models.DeviceGroup.objects.all(), required=False,
                               allow_null=True)
    enrollee = UUIDRelated(queryset=models.Enrollee.objects.all(), required=False,
                           allow_null=True)

    class Meta:
        model = models.Holiday
        fields = ["id", "date", "name", "scope", "device_group", "enrollee", "recurring",
                  "is_cancelled", "is_paid", "metadata", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        scope = attrs.get("scope", getattr(self.instance, "scope", "all"))
        if scope == "device_group" and not attrs.get("device_group",
                                                     getattr(self.instance, "device_group",
                                                             None)):
            raise serializers.ValidationError({"device_group": "required for this scope"})
        if scope == "enrollee" and not attrs.get("enrollee",
                                                 getattr(self.instance, "enrollee", None)):
            raise serializers.ValidationError({"enrollee": "required for this scope"})
        return attrs


class WorkScheduleSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)

    class Meta:
        model = models.WorkSchedule
        fields = ["id", "name", "start_time", "end_time", "crosses_midnight",
                  "grace_in_minutes", "grace_out_minutes", "break_minutes", "weekdays",
                  "metadata", "created_at", "updated_at"]
        read_only_fields = ["crosses_midnight", "created_at", "updated_at"]


class ShiftAssignmentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    schedule = UUIDRelated(queryset=models.WorkSchedule.objects.all())
    enrollee = UUIDRelated(queryset=models.Enrollee.objects.all(), required=False,
                           allow_null=True)
    device_group = UUIDRelated(queryset=models.DeviceGroup.objects.all(), required=False,
                               allow_null=True)

    class Meta:
        model = models.ShiftAssignment
        fields = ["id", "schedule", "enrollee", "device_group", "start_date", "end_date",
                  "priority", "created_at"]
        read_only_fields = ["created_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get("enrollee") and not attrs.get("device_group") and self.instance is None:
            raise serializers.ValidationError("an enrollee or a device group is required")
        return attrs


class WebhookEndpointSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    secret = serializers.CharField(write_only=True, required=False, allow_blank=True)
    has_secret = serializers.SerializerMethodField()

    class Meta:
        model = models.WebhookEndpoint
        fields = ["id", "name", "url", "secret", "has_secret", "events", "headers", "is_active",
                  "description", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def get_has_secret(self, obj: Any) -> bool:
        return bool(obj.secret)


class WebhookDeliverySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    endpoint = UUIDRelated(read_only=True)

    class Meta:
        model = models.WebhookDelivery
        fields = ["id", "endpoint", "event", "payload", "status", "attempts", "response_status",
                  "response_body", "error", "next_attempt_at", "delivered_at", "created_at"]
        read_only_fields = fields


class EnrollmentAgentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)

    class Meta:
        model = models.EnrollmentAgent
        fields = ["id", "name", "algorithm_version", "is_active", "key_prefix", "last_seen_at",
                  "last_ip", "metadata", "created_at"]
        read_only_fields = ["key_prefix", "last_seen_at", "last_ip", "created_at"]


class DeviceEventLogSerializer(serializers.ModelSerializer):
    device = UUIDRelated(read_only=True)

    class Meta:
        model = models.DeviceEventLog
        fields = ["id", "device", "event_type", "op_code", "message", "data", "occurred_at",
                  "created_at"]
        read_only_fields = fields


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.AuditLog
        fields = ["id", "actor_repr", "action", "object_type", "object_id", "object_repr",
                  "metadata", "ip_address", "created_at"]
        read_only_fields = fields


# --------------------------------------------------------------------------- agent API


class AgentSessionSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    enrollee = serializers.SerializerMethodField()

    class Meta:
        model = models.EnrollmentSession
        fields = ["id", "enrollee", "status", "expires_at", "fingers_requested",
                  "fingers_captured"]
        read_only_fields = fields

    def get_enrollee(self, obj: Any) -> dict[str, Any]:
        return {"id": str(obj.enrollee.uuid), "device_pin": obj.enrollee.device_pin,
                "display_name": obj.enrollee.display_name}


class AgentStartSessionSerializer(serializers.Serializer):
    enrollee = serializers.CharField(help_text="Enrollee id (uuid) or device PIN")
    fingers = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=9),
                                    required=False)

    def validate_enrollee(self, value: str) -> Any:
        import uuid as uuid_lib

        qs = models.Enrollee.objects.all()
        try:
            return qs.get(uuid=uuid_lib.UUID(value))
        except (ValueError, models.Enrollee.DoesNotExist):
            pass
        try:
            return qs.get(device_pin=value)
        except models.Enrollee.DoesNotExist:
            raise serializers.ValidationError("enrollee not found") from None


class AgentTemplateUploadSerializer(serializers.Serializer):
    finger_index = serializers.IntegerField(min_value=0, max_value=9)
    algorithm_version = serializers.CharField(max_length=16)
    template = serializers.CharField(help_text="Base64 encoded template")
    quality = serializers.IntegerField(required=False, allow_null=True, min_value=0,
                                       max_value=100)


FINGER_CHOICES = FINGER_NAMES
