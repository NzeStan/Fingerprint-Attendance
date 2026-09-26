"""Abstract base models.

The concrete models in :mod:`fingerprint_attendance.models` are thin subclasses of these, so
projects can reuse the field definitions for their own models (for example an archive table).
Relation fields use fixed ``related_name`` values; override them when you build a second
concrete model from the same base, as Django requires unique reverse accessors.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings as dj_settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ..constants import (
    FINGER_NAMES,
    CommandStatus,
    ConnectionState,
    ConsentState,
    DeliveryStatus,
    DeviceMode,
    DeviceStatus,
    EventLogType,
    HolidayScope,
    Privilege,
    SessionStatus,
    SyncStatus,
    TemplateSource,
)
from ..fields import EmployeeOneToOneField

APP = "fingerprint_attendance"
USER_MODEL = dj_settings.AUTH_USER_MODEL


class ImmutableRecordError(Exception):
    """Raised when code tries to modify an immutable raw record."""


class TimeStampedModel(models.Model):
    uuid = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        abstract = True


# --------------------------------------------------------------------------- devices


class DeviceQuerySet(models.QuerySet):
    def active(self) -> DeviceQuerySet:
        return self.filter(status=DeviceStatus.ACTIVE)

    def online(self) -> DeviceQuerySet:
        from ..conf import settings

        return self.filter(last_seen_at__gte=timezone.now() - settings.DEVICE_OFFLINE_AFTER)

    def offline(self) -> DeviceQuerySet:
        from ..conf import settings

        cutoff = timezone.now() - settings.DEVICE_OFFLINE_AFTER
        return self.filter(models.Q(last_seen_at__lt=cutoff) | models.Q(last_seen_at__isnull=True))


class AbstractDeviceGroup(TimeStampedModel):
    name = models.CharField(_("name"), max_length=100, unique=True)
    description = models.TextField(_("description"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        abstract = True
        ordering = ("name",)
        verbose_name = _("device group")
        verbose_name_plural = _("device groups")

    def __str__(self) -> str:
        return self.name


class AbstractDevice(TimeStampedModel):
    serial_number = models.CharField(_("serial number"), max_length=64, unique=True)
    name = models.CharField(_("name"), max_length=100, blank=True)
    groups = models.ManyToManyField(  # type: ignore[var-annotated]
        f"{APP}.DeviceGroup", related_name="devices", blank=True,
                                    verbose_name=_("groups"))
    location = models.CharField(_("location"), max_length=200, blank=True)
    mode = models.CharField(_("mode"), max_length=8, choices=DeviceMode.choices,
                            default=DeviceMode.PUSH)
    status = models.CharField(_("status"), max_length=20, choices=DeviceStatus.choices,
                              default=DeviceStatus.PENDING_APPROVAL, db_index=True)
    connection_state = models.CharField(_("connection state"), max_length=10,
                                        choices=ConnectionState.choices,
                                        default=ConnectionState.UNKNOWN)
    last_ip = models.GenericIPAddressField(_("last IP"), null=True, blank=True)
    model_name = models.CharField(_("model"), max_length=100, blank=True)
    firmware_version = models.CharField(_("firmware"), max_length=100, blank=True)
    platform = models.CharField(_("platform"), max_length=100, blank=True)
    fp_algorithm_version = models.CharField(_("fingerprint algorithm version"), max_length=16,
                                            blank=True)
    push_version = models.CharField(_("push protocol version"), max_length=32, blank=True)
    timezone = models.CharField(_("timezone"), max_length=64, blank=True,
                                help_text=_("IANA name; blank uses DEFAULT_DEVICE_TIMEZONE."))
    last_seen_at = models.DateTimeField(_("last seen"), null=True, blank=True, db_index=True)
    last_handshake_at = models.DateTimeField(_("last handshake"), null=True, blank=True)
    last_punch_at = models.DateTimeField(_("last punch"), null=True, blank=True)
    went_offline_at = models.DateTimeField(_("went offline at"), null=True, blank=True)
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    approved_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+", verbose_name=_("approved by"))
    capabilities = models.JSONField(_("capabilities"), default=dict, blank=True)
    options = models.JSONField(_("ADMS option overrides"), default=dict, blank=True)
    reported_user_count = models.IntegerField(_("users on device"), null=True, blank=True)
    reported_template_count = models.IntegerField(_("templates on device"), null=True, blank=True)
    reported_punch_count = models.IntegerField(_("punch logs on device"), null=True, blank=True)
    reported_face_count = models.IntegerField(_("faces on device"), null=True, blank=True)
    log_capacity = models.PositiveIntegerField(_("log capacity"), null=True, blank=True)
    log_count_baseline = models.IntegerField(
        _("server punch count at last log clear"), default=0,
        help_text=_("Used to reconcile device-reported counts after the device log is cleared."))
    capacity_warning_active = models.BooleanField(default=False)
    token_hash = models.CharField(_("token hash"), max_length=64, blank=True, editable=False)
    attlog_stamp = models.CharField(_("ATTLOG cursor"), max_length=32, blank=True)
    operlog_stamp = models.CharField(_("OPERLOG cursor"), max_length=32, blank=True)
    attphoto_stamp = models.CharField(_("ATTPHOTO cursor"), max_length=32, blank=True)
    biodata_stamp = models.CharField(_("BIODATA cursor"), max_length=32, blank=True)
    clock_drift_seconds = models.FloatField(_("clock drift (s)"), null=True, blank=True)
    clock_checked_at = models.DateTimeField(null=True, blank=True)
    pull_host = models.CharField(_("pull host"), max_length=255, blank=True)
    pull_port = models.PositiveIntegerField(_("pull port"), null=True, blank=True)
    pull_comm_key = models.PositiveIntegerField(_("pull comm key"), null=True, blank=True)
    pull_last_record_at = models.DateTimeField(_("last imported record (pull)"), null=True,
                                               blank=True)
    notes = models.TextField(_("notes"), blank=True)

    objects = DeviceQuerySet.as_manager()

    class Meta:
        abstract = True
        ordering = ("name", "serial_number")
        verbose_name = _("device")
        verbose_name_plural = _("devices")
        permissions = [
            ("approve_device", "Can approve devices"),
            ("control_device", "Can reboot, set time and resync devices"),
            ("clear_device_logs", "Can clear attendance logs stored on devices"),
            ("clear_device_data", "Can wipe all data stored on devices"),
            ("reupload_device_logs", "Can force devices to re-upload their logs"),
        ]

    def __str__(self) -> str:
        return self.name or self.serial_number

    @property
    def is_online(self) -> bool:
        from ..conf import settings

        if not self.last_seen_at:
            return False
        return bool(timezone.now() - self.last_seen_at <= settings.DEVICE_OFFLINE_AFTER)

    @property
    def offline_duration(self) -> timedelta | None:
        if self.is_online or not self.last_seen_at:
            return None
        return timezone.now() - self.last_seen_at

    @property
    def algorithm_major(self) -> str:
        """``"10.0"`` -> ``"10"``; empty when unknown."""
        return normalize_algorithm(self.fp_algorithm_version)

    @property
    def effective_log_capacity(self) -> int:
        from ..conf import settings

        return int(self.log_capacity or settings.DEFAULT_LOG_CAPACITY)


def normalize_algorithm(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip().lower().replace("zkfinger", "").replace("v", "").strip()
    return text.split(".")[0] if text else ""


# --------------------------------------------------------------------------- enrollees


class EnrolleeQuerySet(models.QuerySet):
    def active(self) -> EnrolleeQuerySet:
        return self.filter(is_active=True)


class AbstractEnrollee(TimeStampedModel):
    employee = EmployeeOneToOneField(related_name="fingerprint_enrollee",
                                     verbose_name=_("employee"))
    device_pin = models.CharField(_("device PIN"), max_length=24, unique=True)
    display_name = models.CharField(_("name on device"), max_length=64, blank=True)
    privilege = models.IntegerField(_("device privilege"), choices=Privilege.choices,
                                    default=Privilege.USER)
    groups = models.ManyToManyField(  # type: ignore[var-annotated]
        f"{APP}.DeviceGroup", related_name="enrollees", blank=True,
                                    verbose_name=_("device groups"))
    is_active = models.BooleanField(_("active"), default=True, db_index=True)
    consent_state = models.CharField(_("consent"), max_length=12, choices=ConsentState.choices,
                                     default=ConsentState.NONE)
    deactivated_at = models.DateTimeField(_("deactivated at"), null=True, blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    objects = EnrolleeQuerySet.as_manager()

    class Meta:
        abstract = True
        ordering = ("device_pin",)
        verbose_name = _("enrollee")
        verbose_name_plural = _("enrollees")

    def __str__(self) -> str:
        return f"{self.display_name or self.employee} ({self.device_pin})"

    @property
    def has_consent(self) -> bool:
        return self.consent_state == ConsentState.GIVEN


class AbstractRetiredPin(models.Model):
    pin = models.CharField(max_length=24, unique=True)
    retired_at = models.DateTimeField(default=timezone.now)
    enrollee_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        abstract = True
        verbose_name = _("retired PIN")


class AbstractConsentRecord(TimeStampedModel):
    enrollee = models.ForeignKey(f"{APP}.Enrollee", on_delete=models.CASCADE,
                                 related_name="consents")
    version = models.CharField(_("consent version"), max_length=50)
    text_reference = models.CharField(_("text reference"), max_length=500, blank=True,
                                      help_text=_("URL or document id of the consent text."))
    method = models.CharField(_("method"), max_length=50, blank=True,
                              help_text=_("e.g. paper, digital, kiosk"))
    given_at = models.DateTimeField(_("given at"), default=timezone.now)
    captured_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    withdrawn_at = models.DateTimeField(_("withdrawn at"), null=True, blank=True)
    withdrawn_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    withdrawal_reason = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True
        ordering = ("-given_at",)
        verbose_name = _("consent record")

    @property
    def is_active(self) -> bool:
        return self.withdrawn_at is None


# --------------------------------------------------------------------------- templates


class AbstractFingerprintTemplate(TimeStampedModel):
    enrollee = models.ForeignKey(f"{APP}.Enrollee", on_delete=models.CASCADE,
                                 related_name="templates")
    finger_index = models.PositiveSmallIntegerField(
        _("finger index"), validators=[MinValueValidator(0), MaxValueValidator(9)])
    algorithm_version = models.CharField(_("algorithm version"), max_length=16)
    template_data = models.TextField(_("stored template"), editable=False)
    size = models.PositiveIntegerField(_("size (bytes)"), default=0)
    checksum = models.CharField(_("SHA-256 of plaintext"), max_length=64)
    quality = models.SmallIntegerField(_("quality"), null=True, blank=True)
    is_valid = models.BooleanField(default=True)
    is_duress = models.BooleanField(default=False)
    source = models.CharField(_("source"), max_length=16, choices=TemplateSource.choices)
    source_device = models.ForeignKey(f"{APP}.Device", null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="+")
    source_agent = models.ForeignKey(f"{APP}.EnrollmentAgent", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    version = models.PositiveIntegerField(_("version"), default=1)

    class Meta:
        abstract = True
        ordering = ("enrollee", "finger_index")
        verbose_name = _("fingerprint template")
        permissions = [
            ("view_template_data", "Can read raw fingerprint template data"),
            ("import_templates", "Can import fingerprint templates"),
        ]

    def __str__(self) -> str:
        return f"{self.enrollee_id}:{self.finger_name} (v{self.algorithm_version})"

    @property
    def finger_name(self) -> str:
        return FINGER_NAMES.get(self.finger_index, str(self.finger_index))

    def get_data(self) -> bytes:
        from ..crypto import get_storage

        return get_storage().load(self)  # type: ignore[arg-type]

    def set_data(self, data: bytes) -> None:
        from ..crypto import get_storage
        from ..utils.security import sha256_hex

        get_storage().save(self, data)  # type: ignore[arg-type]
        self.size = len(data)
        self.checksum = sha256_hex(data)


class AbstractDeviceEnrolleeSync(TimeStampedModel):
    device = models.ForeignKey(f"{APP}.Device", on_delete=models.CASCADE,
                               related_name="sync_records")
    enrollee = models.ForeignKey(f"{APP}.Enrollee", on_delete=models.CASCADE,
                                 related_name="sync_records")
    status = models.CharField(max_length=16, choices=SyncStatus.choices,
                              default=SyncStatus.PENDING, db_index=True)
    user_synced = models.BooleanField(default=False)
    #: ``{"<finger>:<algorithm>": {"version": int, "checksum": str}}`` present on the device
    synced_templates = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        abstract = True
        verbose_name = _("device sync state")

    def __str__(self) -> str:
        return f"{self.device_id}/{self.enrollee_id}: {self.status}"


# --------------------------------------------------------------------------- commands


class AbstractDeviceCommand(TimeStampedModel):
    device = models.ForeignKey(f"{APP}.Device", on_delete=models.CASCADE, related_name="commands")
    command_type = models.CharField(_("type"), max_length=64, db_index=True)
    command_string = models.TextField(
        _("rendered command"), blank=True,
        help_text=_("Commands carrying template data are rendered when sent and stored "
                    "redacted."))
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=CommandStatus.choices,
                              default=CommandStatus.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(null=True, blank=True)
    return_code = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    dedupe_key = models.CharField(max_length=191, blank=True, db_index=True)
    enrollee = models.ForeignKey(f"{APP}.Enrollee", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="commands")
    session = models.ForeignKey(f"{APP}.EnrollmentSession", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="commands")
    created_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        abstract = True
        ordering = ("id",)
        verbose_name = _("device command")
        permissions = [("send_raw_command", "Can send arbitrary commands to devices")]

    def __str__(self) -> str:
        return f"#{self.pk} {self.command_type} -> {self.device_id} [{self.status}]"

    @property
    def command_id(self) -> int:
        """Protocol command id (``C:<id>:...``); unique per device because it is the PK."""
        return int(self.pk)


# --------------------------------------------------------------------------- enrollment


class AbstractEnrollmentAgent(TimeStampedModel):
    name = models.CharField(_("name"), max_length=100)
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    key_prefix = models.CharField(max_length=12, db_index=True, editable=False)
    algorithm_version = models.CharField(_("algorithm version"), max_length=16,
                                         help_text=_("Algorithm of templates this agent "
                                                     "produces (e.g. 10 or 12)."))
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        abstract = True
        verbose_name = _("enrollment agent")

    def __str__(self) -> str:
        return self.name

    # DRF's IsAuthenticated checks ``request.user.is_authenticated``.
    @property
    def is_authenticated(self) -> bool:
        return True


class AbstractEnrollmentSession(TimeStampedModel):
    enrollee = models.ForeignKey(f"{APP}.Enrollee", on_delete=models.CASCADE,
                                 related_name="enrollment_sessions")
    device = models.ForeignKey(f"{APP}.Device", null=True, blank=True, on_delete=models.CASCADE,
                               related_name="enrollment_sessions")
    agent = models.ForeignKey(f"{APP}.EnrollmentAgent", null=True, blank=True,
                              on_delete=models.CASCADE, related_name="sessions")
    status = models.CharField(max_length=12, choices=SessionStatus.choices,
                              default=SessionStatus.PENDING, db_index=True)
    expires_at = models.DateTimeField()
    fingers_requested = models.JSONField(default=list, blank=True)
    fingers_captured = models.JSONField(default=list, blank=True)
    initiated_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True
        ordering = ("-created_at",)
        verbose_name = _("enrollment session")

    def __str__(self) -> str:
        return f"session {self.uuid} [{self.status}]"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


# --------------------------------------------------------------------------- punches


class PunchQuerySet(models.QuerySet):
    def with_flag(self, flag: str) -> PunchQuerySet:
        return self.filter(flags__contains=f"|{flag}|")

    def without_flag(self, flag: str) -> PunchQuerySet:
        return self.exclude(flags__contains=f"|{flag}|")

    def effective(self) -> PunchQuerySet:
        """Punches that count for attendance (soft duplicates excluded)."""
        from ..registry import punch_flags

        qs = self
        for entry in punch_flags:
            if entry.meta.get("exclude_from_processing"):
                qs = qs.without_flag(entry.key)
        return qs


def encode_flags(flags: Any) -> str:
    items = sorted({str(f) for f in flags if f})
    return f"|{'|'.join(items)}|" if items else ""


def decode_flags(value: str) -> list[str]:
    return [f for f in (value or "").split("|") if f]


class AbstractPunch(TimeStampedModel):
    """Immutable raw punch. Corrections are PunchAdjustment rows, never edits."""

    enrollee = models.ForeignKey(f"{APP}.Enrollee", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="punches")
    raw_pin = models.CharField(_("PIN as sent"), max_length=24, db_index=True)
    device = models.ForeignKey(f"{APP}.Device", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="punches")
    punched_at = models.DateTimeField(_("punched at (UTC)"), db_index=True)
    device_local_time = models.CharField(_("device local time as reported"), max_length=32,
                                         blank=True)
    received_at = models.DateTimeField(_("received at"), default=timezone.now, db_index=True)
    verify_mode = models.SmallIntegerField(_("verify mode"), null=True, blank=True)
    raw_state = models.CharField(_("raw state"), max_length=8, blank=True)
    state = models.CharField(_("state"), max_length=32, default="unknown", db_index=True)
    work_code = models.CharField(_("work code"), max_length=32, blank=True)
    raw_payload = models.TextField(_("raw payload"), blank=True)
    dedupe_hash = models.CharField(max_length=64, unique=True)
    source = models.CharField(_("source"), max_length=32, db_index=True)
    flags = models.CharField(_("flags"), max_length=255, blank=True)
    batch_id = models.UUIDField(null=True, blank=True, db_index=True, editable=False)
    created_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    note = models.TextField(blank=True)

    objects = PunchQuerySet.as_manager()

    IMMUTABLE_FIELDS = ("raw_pin", "device_id", "punched_at", "device_local_time",
                        "received_at", "verify_mode", "raw_state", "state", "work_code",
                        "raw_payload", "dedupe_hash", "source", "flags")

    class Meta:
        abstract = True
        ordering = ("-punched_at", "-id")
        verbose_name = _("punch")
        verbose_name_plural = _("punches")
        permissions = [
            ("create_manual_punch", "Can create manual punches"),
            ("import_punches", "Can bulk import punches"),
            ("adjust_punch", "Can record punch adjustments"),
        ]

    def __str__(self) -> str:
        return f"{self.raw_pin} @ {self.punched_at:%Y-%m-%d %H:%M:%S} ({self.state})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk is not None and not getattr(self, "_allow_update", False):
            raise ImmutableRecordError(
                "Punches are immutable; record a PunchAdjustment instead.")
        super().save(*args, **kwargs)

    @property
    def flag_list(self) -> list[str]:
        return decode_flags(self.flags)

    def has_flag(self, flag: str) -> bool:
        return f"|{flag}|" in (self.flags or "")


class AbstractPunchAdjustment(TimeStampedModel):
    class Action(models.TextChoices):
        VOID = "void", _("Void (ignore punch)")
        SET_STATE = "set_state", _("Change state")
        SET_TIME = "set_time", _("Change time")

    punch = models.ForeignKey(f"{APP}.Punch", on_delete=models.CASCADE,
                              related_name="adjustments")
    action = models.CharField(max_length=16, choices=Action.choices)
    new_state = models.CharField(max_length=32, blank=True)
    new_punched_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    created_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        abstract = True
        ordering = ("created_at", "id")
        verbose_name = _("punch adjustment")


# --------------------------------------------------------------------------- attendance


class AbstractAttendanceDay(TimeStampedModel):
    enrollee = models.ForeignKey(f"{APP}.Enrollee", on_delete=models.CASCADE,
                                 related_name="attendance_days")
    work_date = models.DateField(db_index=True)
    first_in = models.DateTimeField(null=True, blank=True)
    last_out = models.DateTimeField(null=True, blank=True)
    worked_duration = models.DurationField(null=True, blank=True)
    punch_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=32, db_index=True)
    statuses = models.JSONField(default=list, blank=True)
    day_type = models.CharField(max_length=32, blank=True, db_index=True)
    day_label = models.CharField(max_length=200, blank=True)
    is_paid = models.BooleanField(default=True)
    expected_shift = models.JSONField(null=True, blank=True)
    computed_at = models.DateTimeField(default=timezone.now)
    processor_version = models.CharField(max_length=64, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True
        ordering = ("-work_date", "enrollee")
        verbose_name = _("attendance day")

    def __str__(self) -> str:
        return f"{self.enrollee_id} {self.work_date}: {self.status}"


# --------------------------------------------------------------------------- calendar


class AbstractHoliday(TimeStampedModel):
    date = models.DateField(db_index=True)
    name = models.CharField(max_length=200)
    scope = models.CharField(max_length=16, choices=HolidayScope.choices,
                             default=HolidayScope.ALL)
    device_group = models.ForeignKey(f"{APP}.DeviceGroup", null=True, blank=True,
                                     on_delete=models.CASCADE, related_name="holidays")
    enrollee = models.ForeignKey(f"{APP}.Enrollee", null=True, blank=True,
                                 on_delete=models.CASCADE, related_name="holidays")
    recurring = models.BooleanField(default=False, help_text=_("Repeats on this day each year."))
    is_cancelled = models.BooleanField(
        default=False,
        help_text=_("Cancels a holiday on this date coming from lower-priority providers "
                    "(e.g. a moved public holiday)."))
    is_paid = models.BooleanField(default=True)
    created_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True
        ordering = ("date",)
        verbose_name = _("holiday")

    def __str__(self) -> str:
        return f"{self.date} {self.name}{' (cancelled)' if self.is_cancelled else ''}"


def _default_weekdays() -> list[int]:
    return [0, 1, 2, 3, 4]


class AbstractWorkSchedule(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    crosses_midnight = models.BooleanField(default=False)
    grace_in_minutes = models.PositiveIntegerField(default=0)
    grace_out_minutes = models.PositiveIntegerField(default=0)
    break_minutes = models.PositiveIntegerField(default=0)
    weekdays = models.JSONField(default=_default_weekdays, blank=True,
                                help_text=_("Weekdays this schedule applies to (Monday=0)."))
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True
        ordering = ("name",)
        verbose_name = _("work schedule")

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.end_time <= self.start_time:
            self.crosses_midnight = True
        super().save(*args, **kwargs)


class AbstractShiftAssignment(TimeStampedModel):
    schedule = models.ForeignKey(f"{APP}.WorkSchedule", on_delete=models.CASCADE,
                                 related_name="assignments")
    enrollee = models.ForeignKey(f"{APP}.Enrollee", null=True, blank=True,
                                 on_delete=models.CASCADE, related_name="shift_assignments")
    device_group = models.ForeignKey(f"{APP}.DeviceGroup", null=True, blank=True,
                                     on_delete=models.CASCADE, related_name="shift_assignments")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    priority = models.IntegerField(default=0, help_text=_("Higher wins when assignments "
                                                          "overlap."))

    class Meta:
        abstract = True
        ordering = ("-priority", "-start_date")
        verbose_name = _("shift assignment")


# --------------------------------------------------------------------------- logs


class AbstractDeviceEventLog(models.Model):
    device = models.ForeignKey(f"{APP}.Device", null=True, blank=True, on_delete=models.CASCADE,
                               related_name="event_logs")
    event_type = models.CharField(max_length=32, choices=EventLogType.choices, db_index=True)
    op_code = models.CharField(max_length=16, blank=True)
    message = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        abstract = True
        ordering = ("-created_at", "-id")
        verbose_name = _("device event")

    def __str__(self) -> str:
        return f"{self.event_type} {self.message[:60]}"


class AbstractAuditLog(models.Model):
    actor = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    actor_repr = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=64, db_index=True)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    object_repr = models.CharField(max_length=200, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        abstract = True
        ordering = ("-created_at", "-id")
        verbose_name = _("audit log entry")
        verbose_name_plural = _("audit log")

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor_repr} {self.action}"


# --------------------------------------------------------------------------- webhooks


class AbstractWebhookEndpoint(TimeStampedModel):
    name = models.CharField(max_length=100)
    url = models.URLField(max_length=500)
    secret = models.CharField(max_length=200, blank=True,
                              help_text=_("Signing secret; blank uses WEBHOOK_SIGNING_SECRET."))
    events = models.JSONField(default=list, blank=True,
                              help_text=_("Subscribed events (empty = all allowed events)."))
    headers = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        abstract = True
        ordering = ("name",)
        verbose_name = _("webhook endpoint")

    def __str__(self) -> str:
        return self.name

    def wants(self, event: str) -> bool:
        return self.is_active and (not self.events or event in self.events)


class AbstractWebhookDelivery(TimeStampedModel):
    endpoint = models.ForeignKey(f"{APP}.WebhookEndpoint", on_delete=models.CASCADE,
                                 related_name="deliveries")
    event = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=DeliveryStatus.choices,
                              default=DeliveryStatus.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    error = models.TextField(blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ("-created_at",)
        verbose_name = _("webhook delivery")
        verbose_name_plural = _("webhook deliveries")
