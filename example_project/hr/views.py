"""A webhook receiver for the package's outbound webhooks (normally in another service)."""

from __future__ import annotations

import json

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from fingerprint_attendance.webhooks.delivery import verify_signature

from .models import WebhookEvent


@csrf_exempt
@require_POST
def webhook_receiver(request: HttpRequest) -> HttpResponse:
    secret = settings.FINGERPRINT_ATTENDANCE["WEBHOOK_SIGNING_SECRET"]
    if not verify_signature(secret, request.headers.get("X-FPA-Timestamp", ""), request.body,
                            request.headers.get("X-FPA-Signature", "")):
        return HttpResponse("bad signature", status=401)
    body = json.loads(request.body)
    # deliveries can be retried: make handling idempotent on the delivery id
    WebhookEvent.objects.get_or_create(delivery_id=body["delivery_id"],
                                       defaults={"event": body["event"],
                                                 "payload": body["data"]})
    return JsonResponse({"ok": True})
