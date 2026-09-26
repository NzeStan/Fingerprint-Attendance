"""Desktop enrollment agent API (``Authorization: Agent <key>``).

The package never talks to USB readers. A local agent built with the vendor SDK (e.g. ZK9500)
captures fingerprints and uses this contract:

1. ``GET  agent/sessions/`` -- open sessions assigned to this agent
   (or ``POST agent/sessions/`` to open one when ``AGENT_CAN_START_SESSIONS``)
2. ``POST agent/sessions/{id}/claim/`` -- mark it in progress
3. ``POST agent/sessions/{id}/templates/`` -- upload each finger (base64, finger index,
   algorithm version, quality)
4. ``POST agent/sessions/{id}/complete/`` -- store and sync; or ``.../cancel/``
"""

from __future__ import annotations

from typing import Any

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import services
from ..conf import settings
from ..constants import OPEN_SESSION_STATUSES
from ..models import EnrollmentSession
from ..utils.timeutils import now
from . import serializers as s
from .authentication import AgentKeyAuthentication, AgentRateThrottle
from .mixins import resolve_exception_handler
from .permissions import IsEnrollmentAgent


class AgentAPIView(APIView):
    authentication_classes = [AgentKeyAuthentication]
    permission_classes = [IsEnrollmentAgent]
    throttle_classes = [AgentRateThrottle]

    def get_exception_handler(self) -> Any:
        return resolve_exception_handler()

    @property
    def agent(self) -> Any:
        return self.request.user

    def get_session(self, session_id: str) -> Any:
        return get_object_or_404(EnrollmentSession.objects.select_related("enrollee", "agent"),
                                 uuid=session_id, agent=self.agent)


class AgentMeView(AgentAPIView):
    def get(self, request: Any) -> Response:
        agent = self.agent
        return Response({
            "id": str(agent.uuid), "name": agent.name,
            "algorithm_version": agent.algorithm_version,
            "allowed_finger_indexes": settings.ALLOWED_FINGER_INDEXES,
            "fingers_required_min": settings.FINGERS_REQUIRED_MIN,
            "fingers_allowed_max": settings.FINGERS_ALLOWED_MAX,
            "can_start_sessions": settings.AGENT_CAN_START_SESSIONS,
            "server_time": now().isoformat(),
        })


class AgentSessionListView(AgentAPIView):
    def get(self, request: Any) -> Response:
        qs = EnrollmentSession.objects.select_related("enrollee").filter(
            agent=self.agent, status__in=OPEN_SESSION_STATUSES, expires_at__gt=now()
        ).order_by("created_at")
        return Response(s.AgentSessionSerializer(qs, many=True).data)

    def post(self, request: Any) -> Response:
        serializer = s.AgentStartSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = services.agent_start_session(self.agent,
                                               serializer.validated_data["enrollee"],
                                               fingers=serializer.validated_data.get("fingers"))
        return Response(s.AgentSessionSerializer(session).data, status=status.HTTP_201_CREATED)


class AgentSessionDetailView(AgentAPIView):
    def get(self, request: Any, session_id: str) -> Response:
        return Response(s.AgentSessionSerializer(self.get_session(session_id)).data)


class AgentSessionClaimView(AgentAPIView):
    def post(self, request: Any, session_id: str) -> Response:
        session = services.claim_session(self.get_session(session_id), self.agent)
        return Response(s.AgentSessionSerializer(session).data)


class AgentTemplateUploadView(AgentAPIView):
    def post(self, request: Any, session_id: str) -> Response:
        session = self.get_session(session_id)
        serializer = s.AgentTemplateUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        result = services.upload_agent_template(
            session, self.agent, finger_index=d["finger_index"],
            algorithm_version=d["algorithm_version"], template=d["template"],
            quality=d.get("quality"))
        return Response(result, status=status.HTTP_201_CREATED)


class AgentSessionCompleteView(AgentAPIView):
    def post(self, request: Any, session_id: str) -> Response:
        session = services.complete_session(self.get_session(session_id), agent=self.agent)
        return Response(s.AgentSessionSerializer(session).data)


class AgentSessionCancelView(AgentAPIView):
    def post(self, request: Any, session_id: str) -> Response:
        session = services.cancel_session(self.get_session(session_id),
                                          reason=request.data.get("reason", "cancelled by agent"),
                                          by=self.agent)
        return Response(s.AgentSessionSerializer(session).data)
