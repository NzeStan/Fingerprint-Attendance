"""Authentication and throttling for desktop enrollment agents::

    Authorization: Agent fpa_xxxxxxxxxxxxxxxx
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.throttling import BaseThrottle

from ..conf import settings
from ..utils import ratelimit
from ..utils.security import client_ip


class AgentKeyAuthentication(BaseAuthentication):
    def authenticate(self, request: Any) -> tuple[Any, str] | None:
        from ..services.enrollment import authenticate_agent

        header = get_authorization_header(request).split()
        scheme = settings.AGENT_AUTH_SCHEME.lower().encode()
        if not header or header[0].lower() != scheme:
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid agent authorization header.")
        agent = authenticate_agent(header[1].decode("utf-8", "ignore"))
        if agent is None:
            raise exceptions.AuthenticationFailed("Invalid or inactive agent key.")
        type(agent).objects.filter(pk=agent.pk).update(
            last_seen_at=timezone.now(), last_ip=client_ip(request._request))
        return agent, "agent"

    def authenticate_header(self, request: Any) -> str:
        return str(settings.AGENT_AUTH_SCHEME)


class AgentRateThrottle(BaseThrottle):
    def allow_request(self, request: Any, view: Any) -> bool:
        agent = getattr(request, "user", None)
        key = f"agent:{getattr(agent, 'pk', client_ip(request._request))}"
        return ratelimit.allow(key, settings.AGENT_RATE_LIMIT)
