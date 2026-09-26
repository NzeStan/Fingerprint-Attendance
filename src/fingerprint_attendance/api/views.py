"""REST API viewsets. Business logic lives in :mod:`fingerprint_attendance.services`."""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import models, services
from ..conf import settings
from ..sync.engine import sync_status_for_device, sync_status_for_enrollee
from . import serializers as s
from .mixins import ConfigurableViewSetMixin
from .schema import OpenApiParameter, extend_schema

P = "fingerprint_attendance"


class BaseViewSet(ConfigurableViewSetMixin, viewsets.GenericViewSet):
    lookup_field = "uuid"
    lookup_url_kwarg = "id"


class ModelViewSet(BaseViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                   mixins.CreateModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin):
    pass


class ReadOnlyViewSet(BaseViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin):
    pass


def _ok(data: Any = None, code: int = status.HTTP_200_OK) -> Response:
    return Response(data if data is not None else {"ok": True}, status=code)


def _command_response(command: Any) -> Response:
    return _ok(s.DeviceCommandSerializer(command).data, status.HTTP_202_ACCEPTED)


# --------------------------------------------------------------------------- device groups


class DeviceGroupViewSet(ModelViewSet):
    serializer_class = s.DeviceGroupSerializer
    filter_spec = {"name": ("name__icontains", "str")}

    def get_queryset(self) -> Any:
        return models.DeviceGroup.objects.annotate(
            device_count=Count("devices", distinct=True),
            enrollee_count=Count("enrollees", distinct=True))

    @action(detail=True, methods=["get"])
    def members(self, request: Any, id: str | None = None) -> Response:
        group = self.get_object()
        return _ok({
            "devices": [{"id": str(d.uuid), "serial_number": d.serial_number, "name": d.name}
                        for d in group.devices.all()],
            "enrollees": [{"id": str(e.uuid), "device_pin": e.device_pin,
                           "display_name": e.display_name} for e in group.enrollees.all()],
        })

    def _change(self, request: Any, relation: str, model: Any, add: bool) -> Response:
        group = self.get_object()
        ids = request.data.get("ids") or []
        if not isinstance(ids, list):
            raise serializers.ValidationError({"ids": "expected a list of ids"})
        objects = list(model.objects.filter(uuid__in=ids))
        manager = getattr(group, relation)
        (manager.add if add else manager.remove)(*objects)
        return _ok({"changed": len(objects)})

    @action(detail=True, methods=["post"], url_path="add-devices")
    def add_devices(self, request: Any, id: str | None = None) -> Response:
        return self._change(request, "devices", models.Device, True)

    @action(detail=True, methods=["post"], url_path="remove-devices")
    def remove_devices(self, request: Any, id: str | None = None) -> Response:
        return self._change(request, "devices", models.Device, False)

    @action(detail=True, methods=["post"], url_path="add-enrollees")
    def add_enrollees(self, request: Any, id: str | None = None) -> Response:
        return self._change(request, "enrollees", models.Enrollee, True)

    @action(detail=True, methods=["post"], url_path="remove-enrollees")
    def remove_enrollees(self, request: Any, id: str | None = None) -> Response:
        return self._change(request, "enrollees", models.Enrollee, False)


# --------------------------------------------------------------------------- devices


class DeviceViewSet(ModelViewSet):
    serializer_class = s.DeviceSerializer
    action_serializers = {"create": s.DeviceCreateSerializer, "send_command":
                          s.SendCommandSerializer}
    action_permissions = {
        "approve": (f"{P}.approve_device",),
        "resync": (f"{P}.control_device",),
        "set_time": (f"{P}.control_device",),
        "reboot": (f"{P}.control_device",),
        "query_users": (f"{P}.control_device",),
        "info": (f"{P}.control_device",),
        "clear_logs": (f"{P}.clear_device_logs",),
        "clear_data": (f"{P}.clear_device_data",),
        "reupload_logs": (f"{P}.reupload_device_logs",),
        "send_command": (f"{P}.send_raw_command",),
        "issue_token": (f"{P}.change_device",),
    }
    filter_spec = {
        "status": ("status", "str"),
        "mode": ("mode", "str"),
        "group": ("groups__uuid", "uuid"),
        "serial_number": ("serial_number", "str"),
        "search": ("name__icontains", "str"),
    }

    def get_queryset(self) -> Any:
        return models.Device.objects.prefetch_related("groups").order_by("name", "serial_number")

    def perform_create(self, serializer: Any) -> None:
        data = dict(serializer.validated_data)
        approve = data.pop("approve", True)
        groups = data.pop("groups", [])
        serial = data.pop("serial_number")
        device = services.register_device(
            serial, status="active" if approve else "pending_approval", by=self.actor, **data)
        if groups:
            device.groups.set(groups)
        serializer.instance = device

    def perform_destroy(self, instance: Any) -> None:
        services.log_action("device.delete", actor=self.actor, obj=instance)
        instance.delete()

    @action(detail=True, methods=["post"])
    def approve(self, request: Any, id: str | None = None) -> Response:
        return _ok(self.get_serializer(services.approve_device(self.get_object(),
                                                                by=self.actor)).data)

    @action(detail=True, methods=["post"])
    def disable(self, request: Any, id: str | None = None) -> Response:
        return _ok(self.get_serializer(services.disable_device(self.get_object(),
                                                                by=self.actor)).data)

    @action(detail=True, methods=["get"], url_path="sync-status")
    def sync_status(self, request: Any, id: str | None = None) -> Response:
        return _ok(sync_status_for_device(self.get_object()))

    @action(detail=True, methods=["post"])
    def resync(self, request: Any, id: str | None = None) -> Response:
        full = bool(request.data.get("full", False))
        return _ok(services.resync_device(self.get_object(), full=full, created_by=self.actor),
                   status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"], url_path="set-time")
    def set_time(self, request: Any, id: str | None = None) -> Response:
        return _command_response(services.queue_command(
            self.get_object(), "set_time", {}, created_by=self.actor, dedupe_key="set_time"))

    @action(detail=True, methods=["post"])
    def reboot(self, request: Any, id: str | None = None) -> Response:
        return _command_response(services.queue_command(
            self.get_object(), "reboot", {}, created_by=self.actor, dedupe_key="reboot"))

    @action(detail=True, methods=["post"])
    def info(self, request: Any, id: str | None = None) -> Response:
        return _command_response(services.queue_command(
            self.get_object(), "info", {}, created_by=self.actor, dedupe_key="info"))

    @action(detail=True, methods=["post"], url_path="query-users")
    def query_users(self, request: Any, id: str | None = None) -> Response:
        pin = request.data.get("pin") or ""
        return _command_response(services.queue_command(
            self.get_object(), "query_users", {"pin": pin} if pin else {},
            created_by=self.actor))

    @action(detail=True, methods=["post"], url_path="clear-logs")
    def clear_logs(self, request: Any, id: str | None = None) -> Response:
        return _command_response(services.clear_device_logs(self.get_object(), by=self.actor))

    @action(detail=True, methods=["post"], url_path="clear-data")
    def clear_data(self, request: Any, id: str | None = None) -> Response:
        if request.data.get("confirm") != self.get_object().serial_number:
            raise serializers.ValidationError(
                {"confirm": "type the device serial number to confirm wiping all data"})
        return _command_response(services.clear_device_data(self.get_object(), by=self.actor))

    @action(detail=True, methods=["post"], url_path="reupload-logs")
    def reupload_logs(self, request: Any, id: str | None = None) -> Response:
        from rest_framework.fields import DateTimeField

        start = request.data.get("from")
        start_dt = DateTimeField().to_internal_value(start) if start else None
        commands = services.reset_upload_cursor(self.get_object(), ("ATTLOG",),
                                                from_datetime=start_dt, by=self.actor)
        return _ok(s.DeviceCommandSerializer(commands, many=True).data,
                   status.HTTP_202_ACCEPTED)

    @extend_schema(request=s.SendCommandSerializer, responses=s.DeviceCommandSerializer)
    @action(detail=True, methods=["post"], url_path="send-command")
    def send_command(self, request: Any, id: str | None = None) -> Response:
        serializer = s.SendCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return _command_response(services.queue_command(
            self.get_object(), data["command_type"], data.get("payload") or {},
            created_by=self.actor, correlation_id=data.get("correlation_id", "")))

    @action(detail=True, methods=["post"], url_path="issue-token")
    def issue_token(self, request: Any, id: str | None = None) -> Response:
        token = services.issue_device_token(self.get_object(), by=self.actor)
        return _ok({"token": token, "note": "Store this token now; it is not shown again."},
                   status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def health(self, request: Any, id: str | None = None) -> Response:
        return _ok(services.device_health(self.get_object()))

    @action(detail=True, methods=["get"])
    def events(self, request: Any, id: str | None = None) -> Response:
        qs = models.DeviceEventLog.objects.filter(device=self.get_object()).order_by(
            "-created_at", "-id")[:200]
        return _ok(s.DeviceEventLogSerializer(qs, many=True).data)

    @action(detail=False, methods=["get"])
    def reconciliation(self, request: Any) -> Response:
        return _ok(services.reconciliation_report(self.filter_queryset(self.get_queryset())))


# --------------------------------------------------------------------------- enrollees


class EnrolleeViewSet(ModelViewSet):
    serializer_class = s.EnrolleeSerializer
    action_permissions = {
        "withdraw_consent": (f"{P}.change_consentrecord",),
        "resync": (f"{P}.change_enrollee",),
    }
    filter_spec = {
        "is_active": ("is_active", "bool"),
        "consent_state": ("consent_state", "str"),
        "group": ("groups__uuid", "uuid"),
        "device_pin": ("device_pin", "str"),
        "search": ("display_name__icontains", "str"),
    }

    def get_queryset(self) -> Any:
        return (models.Enrollee.objects.select_related("employee")
                .prefetch_related("groups", "templates").order_by("device_pin"))

    def perform_destroy(self, instance: Any) -> None:
        services.delete_enrollee(instance, by=self.actor)

    @action(detail=True, methods=["post"])
    def activate(self, request: Any, id: str | None = None) -> Response:
        return _ok(self.get_serializer(services.activate_enrollee(self.get_object(),
                                                                   by=self.actor)).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request: Any, id: str | None = None) -> Response:
        enrollee = services.deactivate_enrollee(self.get_object(), by=self.actor,
                                                reason=request.data.get("reason", ""))
        return _ok(self.get_serializer(enrollee).data)

    @action(detail=True, methods=["get"])
    def fingers(self, request: Any, id: str | None = None) -> Response:
        enrollee = self.get_object()
        templates = s.FingerprintTemplateSerializer(enrollee.templates.all(), many=True).data
        return _ok({"enrolled": sorted({t["finger_index"] for t in templates}),
                    "required_min": settings.FINGERS_REQUIRED_MIN,
                    "allowed_max": settings.FINGERS_ALLOWED_MAX,
                    "allowed_indexes": settings.ALLOWED_FINGER_INDEXES,
                    "templates": templates})

    @action(detail=True, methods=["get"], url_path="sync-status")
    def sync_status(self, request: Any, id: str | None = None) -> Response:
        return _ok(sync_status_for_enrollee(self.get_object()))

    @action(detail=True, methods=["post"])
    def resync(self, request: Any, id: str | None = None) -> Response:
        return _ok(services.resync_enrollee(self.get_object(),
                                            full=bool(request.data.get("full", False)),
                                            created_by=self.actor), status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["get"])
    def consents(self, request: Any, id: str | None = None) -> Response:
        return _ok(s.ConsentRecordSerializer(self.get_object().consents.all(), many=True).data)

    @action(detail=True, methods=["post"], url_path="withdraw-consent")
    def withdraw_consent(self, request: Any, id: str | None = None) -> Response:
        records = services.withdraw_consent(self.get_object(), by=self.actor,
                                            reason=request.data.get("reason", ""))
        return _ok(s.ConsentRecordSerializer(records, many=True).data)


# --------------------------------------------------------------------------- consent


class ConsentRecordViewSet(BaseViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                           mixins.CreateModelMixin):
    serializer_class = s.ConsentRecordSerializer
    action_serializers = {"withdraw": s.WithdrawConsentSerializer}
    filter_spec = {"enrollee": ("enrollee__uuid", "uuid"), "version": ("version", "str")}

    def get_queryset(self) -> Any:
        return models.ConsentRecord.objects.select_related("enrollee").order_by("-given_at")

    @action(detail=False, methods=["post"])
    def withdraw(self, request: Any) -> Response:
        serializer = s.WithdrawConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        records = services.withdraw_consent(serializer.validated_data["enrollee"],
                                            by=self.actor,
                                            reason=serializer.validated_data["reason"])
        return _ok(s.ConsentRecordSerializer(records, many=True).data)


# --------------------------------------------------------------------------- templates


class FingerprintTemplateViewSet(BaseViewSet, mixins.ListModelMixin,
                                 mixins.RetrieveModelMixin, mixins.DestroyModelMixin):
    serializer_class = s.FingerprintTemplateSerializer
    action_serializers = {"import_template": s.TemplateImportSerializer,
                          "data": s.TemplateDataSerializer}
    action_permissions = {"data": (f"{P}.view_template_data",),
                          "import_template": (f"{P}.import_templates",)}
    filter_spec = {
        "enrollee": ("enrollee__uuid", "uuid"),
        "finger_index": ("finger_index", "int"),
        "algorithm_version": ("algorithm_version", "str"),
        "source": ("source", "str"),
    }

    def get_queryset(self) -> Any:
        return models.FingerprintTemplate.objects.select_related(
            "enrollee", "source_device", "source_agent").order_by("enrollee_id", "finger_index")

    def perform_destroy(self, instance: Any) -> None:
        services.delete_template(instance, by=self.actor, reason="api")

    @action(detail=True, methods=["get"])
    def data(self, request: Any, id: str | None = None) -> Response:
        if not settings.EXPOSE_TEMPLATE_DATA_IN_API:
            raise Http404
        serializer = self.get_serializer(self.get_object())
        return _ok(serializer.data)

    @action(detail=False, methods=["post"], url_path="import")
    def import_template(self, request: Any) -> Response:
        from ..constants import TemplateSource

        serializer = s.TemplateImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        template, changed = services.store_template(
            d["enrollee"], d["finger_index"], d["algorithm_version"], d["template"],
            source=TemplateSource.IMPORT, quality=d.get("quality"), by=self.actor)
        return _ok({**s.FingerprintTemplateSerializer(template).data, "changed": changed},
                   status.HTTP_201_CREATED if changed else status.HTTP_200_OK)


# --------------------------------------------------------------------------- sessions


class EnrollmentSessionViewSet(BaseViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                               mixins.CreateModelMixin):
    serializer_class = s.EnrollmentSessionSerializer
    filter_spec = {
        "enrollee": ("enrollee__uuid", "uuid"),
        "device": ("device__uuid", "uuid"),
        "agent": ("agent__uuid", "uuid"),
        "status": ("status", "str"),
    }

    def get_queryset(self) -> Any:
        return models.EnrollmentSession.objects.select_related(
            "enrollee", "device", "agent").order_by("-created_at")

    @action(detail=True, methods=["post"])
    def cancel(self, request: Any, id: str | None = None) -> Response:
        session = services.cancel_session(self.get_object(), by=self.actor,
                                          reason=request.data.get("reason", "cancelled"))
        return _ok(self.get_serializer(session).data)


# --------------------------------------------------------------------------- agents (admin)


class EnrollmentAgentViewSet(ModelViewSet):
    serializer_class = s.EnrollmentAgentSerializer

    def get_queryset(self) -> Any:
        return models.EnrollmentAgent.objects.order_by("name")

    def create(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        agent, key = services.create_agent(
            serializer.validated_data["name"],
            algorithm_version=serializer.validated_data["algorithm_version"],
            metadata=serializer.validated_data.get("metadata"), by=self.actor)
        return _ok({**self.get_serializer(agent).data, "key": key,
                    "note": "Store this key now; it is not shown again."},
                   status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="rotate-key")
    def rotate_key(self, request: Any, id: str | None = None) -> Response:
        key = services.rotate_agent_key(self.get_object(), by=self.actor)
        return _ok({"key": key, "note": "Store this key now; it is not shown again."})


# --------------------------------------------------------------------------- commands


class DeviceCommandViewSet(ReadOnlyViewSet):
    serializer_class = s.DeviceCommandSerializer
    filter_spec = {
        "device": ("device__uuid", "uuid"),
        "status": ("status", "str"),
        "command_type": ("command_type", "str"),
        "correlation_id": ("correlation_id", "str"),
        "enrollee": ("enrollee__uuid", "uuid"),
    }

    def get_queryset(self) -> Any:
        return models.DeviceCommand.objects.select_related("device", "enrollee").order_by("-id")

    @action(detail=True, methods=["post"])
    def cancel(self, request: Any, id: str | None = None) -> Response:
        return _ok(self.get_serializer(services.cancel_command(self.get_object(),
                                                                by=self.actor)).data)

    @action(detail=True, methods=["post"])
    def retry(self, request: Any, id: str | None = None) -> Response:
        return _ok(self.get_serializer(services.retry_command(self.get_object(),
                                                               by=self.actor)).data)


# --------------------------------------------------------------------------- punches


class PunchViewSet(ReadOnlyViewSet):
    serializer_class = s.PunchSerializer
    pagination_setting = "PUNCH_PAGINATION_CLASS"
    action_serializers = {"create_manual": s.ManualPunchSerializer,
                          "import_punches": s.PunchImportSerializer,
                          "adjust": s.PunchAdjustmentSerializer}
    action_permissions = {
        "create_manual": (f"{P}.create_manual_punch",),
        "import_punches": (f"{P}.import_punches",),
        "adjust": (f"{P}.adjust_punch",),
    }
    filter_spec = {
        "enrollee": ("enrollee__uuid", "uuid"),
        "employee": ("enrollee__employee_id", "str"),
        "pin": ("raw_pin", "str"),
        "device": ("device__uuid", "uuid"),
        "device_serial": ("device__serial_number", "str"),
        "group": ("device__groups__uuid", "uuid"),
        "from": ("punched_at__gte", "datetime"),
        "to": ("punched_at__lt", "datetime"),
        "state": ("state", "str"),
        "source": ("source", "str"),
        "flag": ("flags", "flag"),
        "received_after": ("received_at__gte", "datetime"),
    }

    def get_queryset(self) -> Any:
        return models.Punch.objects.select_related("enrollee__employee", "device")

    @extend_schema(parameters=[OpenApiParameter("after", int, OpenApiParameter.QUERY)])
    @action(detail=False, methods=["get"])
    def latest(self, request: Any) -> Response:
        """Polling endpoint: punches with ``sequence`` greater than ``after`` (ascending)."""
        try:
            after = int(request.query_params.get("after", 0))
            limit = min(int(request.query_params.get("limit", 200)), 1000)
        except ValueError:
            raise serializers.ValidationError({"after": "must be an integer"}) from None
        qs = self.filter_queryset(self.get_queryset()).filter(pk__gt=after).order_by("pk")[:limit]
        items = s.PunchSerializer(qs, many=True).data
        cursor = items[-1]["sequence"] if items else after
        return _ok({"results": items, "next_after": cursor})

    @action(detail=False, methods=["post"], url_path="manual")
    def create_manual(self, request: Any) -> Response:
        serializer = s.ManualPunchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        punch = services.create_manual_punch(d["enrollee"], d["punched_at"],
                                             state=d.get("state") or "", device=d.get("device"),
                                             by=self.actor, note=d.get("note", ""))
        return _ok(s.PunchSerializer(punch).data, status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="import")
    def import_punches(self, request: Any) -> Response:
        serializer = s.PunchImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.import_punches(serializer.validated_data["rows"],
                                         source=serializer.validated_data["source"],
                                         by=self.actor)
        return _ok({"created": result.created_count, "duplicates": result.duplicates,
                    "soft_duplicates": result.soft_duplicates,
                    "rejected": [{"pin": c.pin, "reason": r} for c, r in result.rejected]},
                   status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def adjust(self, request: Any, id: str | None = None) -> Response:
        serializer = s.PunchAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        adjustment = services.adjust_punch(self.get_object(), action=d["action"],
                                           new_state=d.get("new_state", ""),
                                           new_punched_at=d.get("new_punched_at"),
                                           reason=d.get("reason", ""), by=self.actor)
        return _ok(s.PunchAdjustmentSerializer(adjustment).data, status.HTTP_201_CREATED)


# --------------------------------------------------------------------------- attendance


class AttendanceDayViewSet(ReadOnlyViewSet):
    serializer_class = s.AttendanceDaySerializer
    action_serializers = {"recompute": s.RecomputeSerializer}
    action_permissions = {"recompute": (f"{P}.change_attendanceday",)}
    filter_spec = {
        "enrollee": ("enrollee__uuid", "uuid"),
        "employee": ("enrollee__employee_id", "str"),
        "group": ("enrollee__groups__uuid", "uuid"),
        "from": ("work_date__gte", "date"),
        "to": ("work_date__lte", "date"),
        "status": ("status", "str"),
        "day_type": ("day_type", "str"),
    }

    def get_queryset(self) -> Any:
        return models.AttendanceDay.objects.select_related("enrollee").order_by(
            "-work_date", "enrollee_id")

    def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        if not settings.ATTENDANCE_PROCESSOR:
            raise Http404("attendance processing is disabled")

    @action(detail=False, methods=["post"])
    def recompute(self, request: Any) -> Response:
        serializer = s.RecomputeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        ids = [e.pk for e in d["enrollees"]] if d.get("enrollees") else None
        count = services.recompute(start=d["start"], end=d["end"], enrollee_ids=ids)
        return _ok({"days_computed": count})


# --------------------------------------------------------------------------- calendar


class HolidayViewSet(ModelViewSet):
    serializer_class = s.HolidaySerializer
    filter_spec = {"from": ("date__gte", "date"), "to": ("date__lte", "date"),
                   "scope": ("scope", "str"), "recurring": ("recurring", "bool")}

    def get_queryset(self) -> Any:
        return models.Holiday.objects.select_related("device_group", "enrollee").order_by("date")

    def perform_create(self, serializer: Any) -> None:
        serializer.save(created_by=self.actor if getattr(self.actor, "pk", None) else None)


class _ScheduleGate:
    def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)  # type: ignore[misc]
        if not settings.SCHEDULE_MODELS_ENABLED:
            raise Http404("schedule models are disabled (SCHEDULE_MODELS_ENABLED)")


class WorkScheduleViewSet(_ScheduleGate, ModelViewSet):
    serializer_class = s.WorkScheduleSerializer

    def get_queryset(self) -> Any:
        return models.WorkSchedule.objects.order_by("name")


class ShiftAssignmentViewSet(_ScheduleGate, ModelViewSet):
    serializer_class = s.ShiftAssignmentSerializer
    filter_spec = {"enrollee": ("enrollee__uuid", "uuid"),
                   "device_group": ("device_group__uuid", "uuid"),
                   "schedule": ("schedule__uuid", "uuid")}

    def get_queryset(self) -> Any:
        return models.ShiftAssignment.objects.select_related(
            "schedule", "enrollee", "device_group")


class CalendarView(ConfigurableViewSetMixin, APIView):
    """Resolved calendar days, using exactly the logic the processor uses."""

    basename = "calendar"
    action = "list"

    @extend_schema(parameters=[
        OpenApiParameter("enrollee", str, OpenApiParameter.QUERY),
        OpenApiParameter("from", date, OpenApiParameter.QUERY),
        OpenApiParameter("to", date, OpenApiParameter.QUERY),
    ])
    def get(self, request: Any) -> Response:
        from ..calendar.providers import get_calendar_provider, get_schedule_provider
        from ..utils.timeutils import daterange

        enrollee = get_object_or_404(models.Enrollee, uuid=request.query_params.get("enrollee"))
        start = parse_date(request.query_params.get("from") or "")
        end = parse_date(request.query_params.get("to") or "")
        if not start or not end or end < start:
            raise serializers.ValidationError({"from": "from/to dates are required"})
        if (end - start).days > 366:
            raise serializers.ValidationError({"to": "range is limited to 366 days"})
        days = get_calendar_provider().get_range(enrollee, start, end)[enrollee.pk]
        schedules = get_schedule_provider()
        return _ok({
            "enrollee": str(enrollee.uuid),
            "days": [{**days[d].to_dict(),
                      "expected_shift": (lambda sh: sh.to_dict() if sh else None)(
                          schedules.get_expected_shift(enrollee, d))}
                     for d in daterange(start, end)],
        })


# --------------------------------------------------------------------------- health


class HealthView(ConfigurableViewSetMixin, APIView):
    basename = "health"
    action = "list"

    def get(self, request: Any) -> Response:
        return _ok(services.health_summary())


# --------------------------------------------------------------------------- webhooks


class WebhookEndpointViewSet(ModelViewSet):
    serializer_class = s.WebhookEndpointSerializer

    def get_queryset(self) -> Any:
        return models.WebhookEndpoint.objects.order_by("name")

    def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        if not settings.WEBHOOKS_ENABLED:
            raise Http404("webhooks are disabled (WEBHOOKS_ENABLED)")

    def perform_create(self, serializer: Any) -> None:
        serializer.save(created_by=self.actor if getattr(self.actor, "pk", None) else None)

    @action(detail=True, methods=["post"])
    def test(self, request: Any, id: str | None = None) -> Response:
        from ..webhooks.delivery import send_test

        return _ok(s.WebhookDeliverySerializer(send_test(self.get_object())).data)

    @action(detail=True, methods=["get"])
    def deliveries(self, request: Any, id: str | None = None) -> Response:
        qs = self.get_object().deliveries.order_by("-created_at")[:200]
        return _ok(s.WebhookDeliverySerializer(qs, many=True).data)


class WebhookDeliveryViewSet(ReadOnlyViewSet):
    serializer_class = s.WebhookDeliverySerializer
    filter_spec = {"endpoint": ("endpoint__uuid", "uuid"), "status": ("status", "str"),
                   "event": ("event", "str")}

    def get_queryset(self) -> Any:
        return models.WebhookDelivery.objects.select_related("endpoint").order_by("-created_at")

    @action(detail=True, methods=["post"])
    def redeliver(self, request: Any, id: str | None = None) -> Response:
        from ..webhooks.delivery import deliver

        delivery = self.get_object()
        deliver(delivery.pk)
        delivery.refresh_from_db()
        return _ok(self.get_serializer(delivery).data)


# --------------------------------------------------------------------------- logs


class DeviceEventLogViewSet(ReadOnlyViewSet):
    serializer_class = s.DeviceEventLogSerializer
    lookup_field = "pk"
    filter_spec = {"device": ("device__uuid", "uuid"), "event_type": ("event_type", "str"),
                   "from": ("created_at__gte", "datetime")}

    def get_queryset(self) -> Any:
        return models.DeviceEventLog.objects.select_related("device").order_by("-created_at",
                                                                               "-id")


class AuditLogViewSet(ReadOnlyViewSet):
    serializer_class = s.AuditLogSerializer
    lookup_field = "pk"
    filter_spec = {"action": ("action", "str"), "object_id": ("object_id", "str"),
                   "from": ("created_at__gte", "datetime")}

    def get_queryset(self) -> Any:
        return models.AuditLog.objects.order_by("-created_at", "-id")

