"""REST API URLs (versioned). Include with the configured namespace::

    path("api/fingerprint/", include("fingerprint_attendance.api.urls"))

or simply include ``fingerprint_attendance.urls``. Viewsets can be swapped with
``VIEWSET_OVERRIDES = {"devices": "myapp.api.MyDeviceViewSet"}``.
"""

from __future__ import annotations

from typing import Any

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from ..conf import import_object, settings
from . import agent, views

app_name = "fingerprint_attendance_api"

DEFAULT_VIEWSETS: dict[str, Any] = {
    "device-groups": views.DeviceGroupViewSet,
    "devices": views.DeviceViewSet,
    "enrollees": views.EnrolleeViewSet,
    "consents": views.ConsentRecordViewSet,
    "templates": views.FingerprintTemplateViewSet,
    "enrollment-sessions": views.EnrollmentSessionViewSet,
    "enrollment-agents": views.EnrollmentAgentViewSet,
    "commands": views.DeviceCommandViewSet,
    "punches": views.PunchViewSet,
    "attendance-days": views.AttendanceDayViewSet,
    "holidays": views.HolidayViewSet,
    "work-schedules": views.WorkScheduleViewSet,
    "shift-assignments": views.ShiftAssignmentViewSet,
    "webhooks": views.WebhookEndpointViewSet,
    "webhook-deliveries": views.WebhookDeliveryViewSet,
    "device-events": views.DeviceEventLogViewSet,
    "audit-log": views.AuditLogViewSet,
}


def build_router() -> DefaultRouter:
    router = DefaultRouter()
    overrides = settings.VIEWSET_OVERRIDES or {}
    for prefix, viewset in DEFAULT_VIEWSETS.items():
        basename = prefix.replace("-", "_")
        chosen = overrides.get(basename) or overrides.get(prefix) or viewset
        router.register(prefix, import_object(chosen, setting="VIEWSET_OVERRIDES"),
                        basename=basename)
    return router


router = build_router()

agent_patterns: list[Any] = [
    path("me/", agent.AgentMeView.as_view(), name="agent-me"),
    path("sessions/", agent.AgentSessionListView.as_view(), name="agent-sessions"),
    path("sessions/<uuid:session_id>/", agent.AgentSessionDetailView.as_view(),
         name="agent-session"),
    path("sessions/<uuid:session_id>/claim/", agent.AgentSessionClaimView.as_view(),
         name="agent-session-claim"),
    path("sessions/<uuid:session_id>/templates/", agent.AgentTemplateUploadView.as_view(),
         name="agent-session-templates"),
    path("sessions/<uuid:session_id>/complete/", agent.AgentSessionCompleteView.as_view(),
         name="agent-session-complete"),
    path("sessions/<uuid:session_id>/cancel/", agent.AgentSessionCancelView.as_view(),
         name="agent-session-cancel"),
]

v1_patterns: list[Any] = [
    path("health/", views.HealthView.as_view(), name="health"),
    path("calendar/", views.CalendarView.as_view(), name="calendar"),
    path("agent/", include(agent_patterns)),
    path("", include(router.urls)),
]

urlpatterns = [path("v1/", include(v1_patterns))]
