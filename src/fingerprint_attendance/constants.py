"""Closed enumerations (open ones live in :mod:`fingerprint_attendance.registry`)."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class DeviceStatus(models.TextChoices):
    PENDING_APPROVAL = "pending_approval", _("Pending approval")
    ACTIVE = "active", _("Active")
    DISABLED = "disabled", _("Disabled")


class ConnectionState(models.TextChoices):
    UNKNOWN = "unknown", _("Unknown")
    ONLINE = "online", _("Online")
    OFFLINE = "offline", _("Offline")


class DeviceMode(models.TextChoices):
    PUSH = "push", _("ADMS push")
    PULL = "pull", _("Pull (LAN)")


class CommandStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SENT = "sent", _("Sent")
    SUCCEEDED = "succeeded", _("Succeeded")
    FAILED = "failed", _("Failed")
    EXPIRED = "expired", _("Expired")
    CANCELLED = "cancelled", _("Cancelled")


OPEN_COMMAND_STATUSES = (CommandStatus.PENDING, CommandStatus.SENT)
FINAL_COMMAND_STATUSES = (
    CommandStatus.SUCCEEDED,
    CommandStatus.FAILED,
    CommandStatus.EXPIRED,
    CommandStatus.CANCELLED,
)


class SessionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In progress")
    COMPLETED = "completed", _("Completed")
    EXPIRED = "expired", _("Expired")
    CANCELLED = "cancelled", _("Cancelled")
    FAILED = "failed", _("Failed")


OPEN_SESSION_STATUSES = (SessionStatus.PENDING, SessionStatus.IN_PROGRESS)


class TemplateSource(models.TextChoices):
    DEVICE = "device", _("Device")
    DESKTOP_AGENT = "desktop_agent", _("Desktop agent")
    IMPORT = "import", _("Import")


class ConsentState(models.TextChoices):
    NONE = "none", _("Not given")
    GIVEN = "given", _("Given")
    WITHDRAWN = "withdrawn", _("Withdrawn")


class SyncStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SYNCED = "synced", _("Synced")
    PARTIAL = "partial", _("Partially synced")
    INCOMPATIBLE = "incompatible", _("Incompatible algorithm")
    FAILED = "failed", _("Failed")
    DELETING = "deleting", _("Deleting")
    DELETED = "deleted", _("Deleted")
    OUT_OF_SCOPE = "out_of_scope", _("Out of scope")


class Privilege(models.IntegerChoices):
    """ZKTeco privilege levels."""

    USER = 0, _("User")
    ENROLLER = 2, _("Enroller")
    ADMIN = 6, _("Administrator")
    SUPER_ADMIN = 14, _("Super administrator")


class HolidayScope(models.TextChoices):
    ALL = "all", _("Everyone")
    DEVICE_GROUP = "device_group", _("Device group")
    ENROLLEE = "enrollee", _("Enrollee")


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SUCCEEDED = "succeeded", _("Succeeded")
    FAILED = "failed", _("Failed")
    RETRYING = "retrying", _("Retrying")


class EventLogType(models.TextChoices):
    HANDSHAKE = "handshake", _("Handshake")
    OPERLOG = "operlog", _("Operation log")
    USER_INFO = "user_info", _("User info")
    TEMPLATE_RECEIVED = "template_received", _("Template received")
    TEMPLATE_REJECTED = "template_rejected", _("Template rejected")
    PARSE_ERROR = "parse_error", _("Parse error")
    CLOCK_DRIFT = "clock_drift", _("Clock drift")
    CAPACITY_WARNING = "capacity_warning", _("Capacity warning")
    COMMAND_RESULT = "command_result", _("Command result")
    AUTH_FAILED = "auth_failed", _("Authentication failed")
    INFO = "info", _("Device info")
    CURSOR_RESET = "cursor_reset", _("Upload cursor reset")
    ERROR = "error", _("Error")
    OTHER = "other", _("Other")


#: ZKTeco finger index names (0-9).
FINGER_NAMES = {
    0: "left_little",
    1: "left_ring",
    2: "left_middle",
    3: "left_index",
    4: "left_thumb",
    5: "right_thumb",
    6: "right_index",
    7: "right_middle",
    8: "right_ring",
    9: "right_little",
}

#: Upload cursor tables tracked per device.
CURSOR_TABLES = ("ATTLOG", "OPERLOG", "ATTPHOTO", "BIODATA")
