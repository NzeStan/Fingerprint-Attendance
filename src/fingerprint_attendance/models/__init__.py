"""Concrete models. See :mod:`fingerprint_attendance.models.abstract` for the field
definitions (reusable as abstract bases)."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from .abstract import (
    AbstractAttendanceDay,
    AbstractAuditLog,
    AbstractConsentRecord,
    AbstractDevice,
    AbstractDeviceCommand,
    AbstractDeviceEnrolleeSync,
    AbstractDeviceEventLog,
    AbstractDeviceGroup,
    AbstractEnrollee,
    AbstractEnrollmentAgent,
    AbstractEnrollmentSession,
    AbstractFingerprintTemplate,
    AbstractHoliday,
    AbstractPunch,
    AbstractPunchAdjustment,
    AbstractRetiredPin,
    AbstractShiftAssignment,
    AbstractWebhookDelivery,
    AbstractWebhookEndpoint,
    AbstractWorkSchedule,
    ImmutableRecordError,
    decode_flags,
    encode_flags,
    normalize_algorithm,
)

__all__ = [
    "AttendanceDay",
    "AuditLog",
    "ConsentRecord",
    "Device",
    "DeviceCommand",
    "DeviceEnrolleeSync",
    "DeviceEventLog",
    "DeviceGroup",
    "Enrollee",
    "EnrollmentAgent",
    "EnrollmentSession",
    "FingerprintTemplate",
    "Holiday",
    "ImmutableRecordError",
    "Punch",
    "PunchAdjustment",
    "RetiredPin",
    "ShiftAssignment",
    "WebhookDelivery",
    "WebhookEndpoint",
    "WorkSchedule",
    "decode_flags",
    "encode_flags",
    "normalize_algorithm",
]


class DeviceGroup(AbstractDeviceGroup):
    class Meta(AbstractDeviceGroup.Meta):
        pass


class Device(AbstractDevice):
    class Meta(AbstractDevice.Meta):
        indexes = [models.Index(fields=["status", "last_seen_at"], name="fpa_device_status_seen")]


class Enrollee(AbstractEnrollee):
    class Meta(AbstractEnrollee.Meta):
        pass


class RetiredPin(AbstractRetiredPin):
    class Meta(AbstractRetiredPin.Meta):
        pass


class ConsentRecord(AbstractConsentRecord):
    class Meta(AbstractConsentRecord.Meta):
        indexes = [models.Index(fields=["enrollee", "withdrawn_at"], name="fpa_consent_active")]


class EnrollmentAgent(AbstractEnrollmentAgent):
    class Meta(AbstractEnrollmentAgent.Meta):
        pass


class FingerprintTemplate(AbstractFingerprintTemplate):
    class Meta(AbstractFingerprintTemplate.Meta):
        constraints = [
            models.UniqueConstraint(fields=["enrollee", "finger_index", "algorithm_version"],
                                    name="fpa_template_unique_finger_algo"),
            models.CheckConstraint(condition=Q(finger_index__gte=0, finger_index__lte=9),
                                   name="fpa_template_finger_range"),
        ]


class DeviceEnrolleeSync(AbstractDeviceEnrolleeSync):
    class Meta(AbstractDeviceEnrolleeSync.Meta):
        constraints = [
            models.UniqueConstraint(fields=["device", "enrollee"], name="fpa_sync_unique"),
        ]


class EnrollmentSession(AbstractEnrollmentSession):
    class Meta(AbstractEnrollmentSession.Meta):
        constraints = [
            models.CheckConstraint(
                condition=(Q(device__isnull=False, agent__isnull=True)
                           | Q(device__isnull=True, agent__isnull=False)),
                name="fpa_session_one_target",
            ),
        ]


class DeviceCommand(AbstractDeviceCommand):
    class Meta(AbstractDeviceCommand.Meta):
        indexes = [
            models.Index(fields=["device", "status", "id"], name="fpa_cmd_device_queue"),
            models.Index(fields=["status", "next_attempt_at"], name="fpa_cmd_retry"),
        ]


class Punch(AbstractPunch):
    class Meta(AbstractPunch.Meta):
        indexes = [
            models.Index(fields=["enrollee", "punched_at"], name="fpa_punch_enrollee_time"),
            models.Index(fields=["device", "punched_at"], name="fpa_punch_device_time"),
            models.Index(fields=["raw_pin", "punched_at"], name="fpa_punch_pin_time"),
        ]


class PunchAdjustment(AbstractPunchAdjustment):
    class Meta(AbstractPunchAdjustment.Meta):
        pass


class AttendanceDay(AbstractAttendanceDay):
    class Meta(AbstractAttendanceDay.Meta):
        constraints = [
            models.UniqueConstraint(fields=["enrollee", "work_date"], name="fpa_day_unique"),
        ]
        indexes = [models.Index(fields=["work_date", "status"], name="fpa_day_date_status")]


class Holiday(AbstractHoliday):
    class Meta(AbstractHoliday.Meta):
        pass


class WorkSchedule(AbstractWorkSchedule):
    class Meta(AbstractWorkSchedule.Meta):
        pass


class ShiftAssignment(AbstractShiftAssignment):
    class Meta(AbstractShiftAssignment.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(enrollee__isnull=False) | Q(device_group__isnull=False),
                name="fpa_shift_has_target",
            ),
        ]


class DeviceEventLog(AbstractDeviceEventLog):
    class Meta(AbstractDeviceEventLog.Meta):
        pass


class AuditLog(AbstractAuditLog):
    class Meta(AbstractAuditLog.Meta):
        pass


class WebhookEndpoint(AbstractWebhookEndpoint):
    class Meta(AbstractWebhookEndpoint.Meta):
        pass


class WebhookDelivery(AbstractWebhookDelivery):
    class Meta(AbstractWebhookDelivery.Meta):
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="fpa_webhook_retry")]
