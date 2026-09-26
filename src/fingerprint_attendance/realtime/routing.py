"""Include in your ASGI router::

    from fingerprint_attendance.realtime.routing import websocket_urlpatterns
    URLRouter(websocket_urlpatterns + your_patterns)
"""

from __future__ import annotations

from django.urls import path

from .consumers import EventsConsumer

websocket_urlpatterns = [
    path("ws/fingerprint-attendance/events/", EventsConsumer.as_asgi()),
]
