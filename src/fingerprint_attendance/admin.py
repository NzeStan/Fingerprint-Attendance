"""Django admin. Raw punches are read-only; templates show metadata only (never bytes)."""

from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from . import models, services
from .conf import settings
from .exceptions import FPAError


def _run(modeladmin: admin.ModelAdmin, request: HttpRequest, queryset: QuerySet,
         func: Any, label: str) -> None:
    ok, failed = 0, 0
    for obj in queryset:
        try:
            func(obj)
            ok += 1
        except FPAError as exc:
            failed += 1
            modeladmin.message_user(request, f"{obj}: {exc.message}", messages.ERROR)
    if ok:
        modeladmin.message_user(request, f"{label}: {ok} done.", messages.SUCCESS)


class ReadOnlyAdminMixin:
    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(models.DeviceGroup)
class DeviceGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "created_at")
    search_fields = ("name",)


@admin.register(models.Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("serial_number", "name", "status", "online", "last_seen_at", "location",
                    "fp_algorithm_version", "reported_punch_count", "clock_drift_seconds")
    list_filter = ("status", "mode", "groups")
    search_fields = ("serial_number", "name", "location")
    filter_horizontal = ("groups",)
    readonly_fields = (
        "uuid", "connection_state", "last_ip", "model_name", "firmware_version", "platform",
        "push_version", "last_seen_at", "last_handshake_at", "last_punch_at", "went_offline_at",
        "approved_at", "approved_by", "capabilities", "reported_user_count",
        "reported_template_count", "reported_punch_count", "reported_face_count",
        "log_count_baseline", "capacity_warning_active", "attlog_stamp", "operlog_stamp",
        "attphoto_stamp", "biodata_stamp", "clock_drift_seconds", "clock_checked_at",
        "pull_last_record_at", "created_at", "updated_at",
    )
    exclude = ("token_hash",)
    actions = ["approve", "disable", "resync", "full_resync", "request_info", "sync_time",
               "force_reupload"]

    @admin.display(boolean=True, description=_("online"))
    def online(self, obj: Any) -> bool:
        return bool(obj.is_online)

    @admin.action(description=_("Approve selected devices"), permissions=["change"])
    def approve(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset, lambda d: services.approve_device(d, by=request.user),
             "Approved")

    @admin.action(description=_("Disable selected devices"), permissions=["change"])
    def disable(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset, lambda d: services.disable_device(d, by=request.user),
             "Disabled")

    @admin.action(description=_("Resync missing users/templates"), permissions=["change"])
    def resync(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset, lambda d: services.resync_device(d), "Resync queued")

    @admin.action(description=_("Full resync (push everything again)"), permissions=["change"])
    def full_resync(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset, lambda d: services.resync_device(d, full=True),
             "Full resync queued")

    @admin.action(description=_("Ask device for its info / counts"), permissions=["change"])
    def request_info(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset,
             lambda d: services.queue_command(d, "info", {}, created_by=request.user,
                                              dedupe_key="info"), "INFO queued")

    @admin.action(description=_("Set device clock to server time"), permissions=["change"])
    def sync_time(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset,
             lambda d: services.queue_command(d, "set_time", {}, created_by=request.user,
                                              dedupe_key="set_time"), "Set time queued")

    @admin.action(description=_("Force re-upload of stored punches"), permissions=["change"])
    def force_reupload(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset,
             lambda d: services.reset_upload_cursor(d, ("ATTLOG",), by=request.user),
             "Re-upload requested")


class ConsentInline(admin.TabularInline):
    model = models.ConsentRecord
    extra = 0
    fields = ("version", "method", "given_at", "withdrawn_at", "withdrawal_reason")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class TemplateInline(admin.TabularInline):
    model = models.FingerprintTemplate
    extra = 0
    fields = ("finger_index", "algorithm_version", "version", "size", "quality", "source",
              "updated_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(models.Enrollee)
class EnrolleeAdmin(admin.ModelAdmin):
    list_display = ("device_pin", "display_name", "employee", "is_active", "consent_state",
                    "finger_count")
    list_filter = ("is_active", "consent_state", "groups")
    search_fields = ("device_pin", "display_name")
    filter_horizontal = ("groups",)
    readonly_fields = ("uuid", "consent_state", "deactivated_at", "created_at", "updated_at")
    raw_id_fields = ("employee",)
    inlines = [TemplateInline, ConsentInline]
    actions = ["deactivate", "activate", "resync"]

    @admin.display(description=_("fingers"))
    def finger_count(self, obj: Any) -> int:
        return int(obj.templates.count())

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        services.delete_enrollee(obj, by=request.user)

    @admin.action(description=_("Deactivate (removes from devices)"), permissions=["change"])
    def deactivate(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset,
             lambda e: services.deactivate_enrollee(e, by=request.user), "Deactivated")

    @admin.action(description=_("Activate"), permissions=["change"])
    def activate(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset, lambda e: services.activate_enrollee(e, by=request.user),
             "Activated")

    @admin.action(description=_("Resync to devices"), permissions=["change"])
    def resync(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset, lambda e: services.resync_enrollee(e), "Resync queued")


@admin.register(models.ConsentRecord)
class ConsentRecordAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("enrollee", "version", "method", "given_at", "withdrawn_at")
    list_filter = ("version", "method")
    search_fields = ("enrollee__device_pin", "enrollee__display_name")


@admin.register(models.FingerprintTemplate)
class FingerprintTemplateAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("enrollee", "finger_index", "algorithm_version", "version", "size",
                    "quality", "source", "updated_at")
    list_filter = ("algorithm_version", "source")
    search_fields = ("enrollee__device_pin", "enrollee__display_name")
    # template_data is deliberately excluded
    fields = ("uuid", "enrollee", "finger_index", "algorithm_version", "version", "size",
              "checksum", "quality", "is_valid", "is_duress", "source", "source_device",
              "source_agent", "created_at", "updated_at")
    readonly_fields = fields

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        services.delete_template(obj, by=request.user, reason="admin")


@admin.register(models.DeviceEnrolleeSync)
class DeviceEnrolleeSyncAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("device", "enrollee", "status", "user_synced", "last_synced_at")
    list_filter = ("status", "device")
    search_fields = ("enrollee__device_pin", "device__serial_number")


@admin.register(models.DeviceCommand)
class DeviceCommandAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "device", "command_type", "status", "attempts", "return_code",
                    "created_at", "sent_at", "completed_at")
    list_filter = ("status", "command_type")
    search_fields = ("device__serial_number", "correlation_id")
    actions = ["cancel", "retry"]

    @admin.action(description=_("Cancel selected commands"))
    def cancel(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset, lambda c: services.cancel_command(c, by=request.user),
             "Cancelled")

    @admin.action(description=_("Retry selected commands"))
    def retry(self, request: HttpRequest, queryset: QuerySet) -> None:
        _run(self, request, queryset, lambda c: services.retry_command(c, by=request.user),
             "Retried")


@admin.register(models.EnrollmentAgent)
class EnrollmentAgentAdmin(admin.ModelAdmin):
    list_display = ("name", "algorithm_version", "is_active", "key_prefix", "last_seen_at")
    readonly_fields = ("uuid", "key_prefix", "last_seen_at", "last_ip", "created_at")
    exclude = ("key_hash",)

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        if not change:
            from .services.enrollment import create_agent

            agent, key = create_agent(obj.name, algorithm_version=obj.algorithm_version,
                                      by=request.user, metadata=obj.metadata)
            obj.pk, obj.uuid = agent.pk, agent.uuid
            self.message_user(request, f"Agent key (shown once): {key}", messages.WARNING)
            return
        super().save_model(request, obj, form, change)


@admin.register(models.EnrollmentSession)
class EnrollmentSessionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("uuid", "enrollee", "device", "agent", "status", "expires_at")
    list_filter = ("status",)
    exclude = ("metadata",)  # may hold staged (encrypted) templates


@admin.register(models.Punch)
class PunchAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("punched_at", "raw_pin", "enrollee", "device", "state", "source", "flags")
    list_filter = ("source", "state", "device")
    search_fields = ("raw_pin", "enrollee__display_name")
    date_hierarchy = "punched_at"
    list_select_related = ("enrollee", "device")

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(models.PunchAdjustment)
class PunchAdjustmentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("punch", "action", "new_state", "new_punched_at", "created_at")


@admin.register(models.AttendanceDay)
class AttendanceDayAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("work_date", "enrollee", "status", "first_in", "last_out",
                    "worked_duration", "day_type")
    list_filter = ("status", "day_type")
    date_hierarchy = "work_date"
    search_fields = ("enrollee__device_pin", "enrollee__display_name")


@admin.register(models.Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name", "scope", "recurring", "is_cancelled", "is_paid")
    list_filter = ("scope", "recurring", "is_cancelled")
    search_fields = ("name",)


if settings.SCHEDULE_MODELS_ENABLED:
    @admin.register(models.WorkSchedule)
    class WorkScheduleAdmin(admin.ModelAdmin):
        list_display = ("name", "start_time", "end_time", "crosses_midnight")

    @admin.register(models.ShiftAssignment)
    class ShiftAssignmentAdmin(admin.ModelAdmin):
        list_display = ("schedule", "enrollee", "device_group", "start_date", "end_date",
                        "priority")


@admin.register(models.DeviceEventLog)
class DeviceEventLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("created_at", "device", "event_type", "op_code", "message")
    list_filter = ("event_type",)


@admin.register(models.AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("created_at", "actor_repr", "action", "object_type", "object_repr")
    list_filter = ("action",)
    search_fields = ("actor_repr", "object_repr", "object_id")

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(models.WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ("name", "url", "is_active")


@admin.register(models.WebhookDelivery)
class WebhookDeliveryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("created_at", "endpoint", "event", "status", "attempts", "response_status")
    list_filter = ("status", "event")
