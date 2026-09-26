"""Outbound webhooks: JSON body, HMAC-SHA256 signature, timestamp, retries with backoff.

Headers sent with every delivery::

    X-FPA-Event: punch_received
    X-FPA-Delivery: <uuid>
    X-FPA-Timestamp: <unix seconds>
    X-FPA-Signature: sha256=<hex HMAC of "<timestamp>.<body>">

Verify with :func:`verify_signature` (reject old timestamps to prevent replays).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import timedelta
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder

from ..conf import settings
from ..constants import DeliveryStatus
from ..utils.logging import get_logger
from ..utils.timeutils import now

logger = get_logger(__name__)

RESPONSE_LIMIT = 2000


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str, *,
                     tolerance_seconds: int = 300) -> bool:
    """Helper for webhook receivers."""
    try:
        if abs(time.time() - int(timestamp)) > tolerance_seconds:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def urllib_sender(url: str, body: bytes, headers: dict[str, str],
                  timeout: int) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read(RESPONSE_LIMIT).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(RESPONSE_LIMIT).decode("utf-8", "replace")


def event_allowed(event: str) -> bool:
    allow = settings.WEBHOOK_EVENTS or []
    return not allow or event in allow


def queue_event(event: str, payload: dict[str, Any]) -> list[Any]:
    """Create deliveries for every subscribed endpoint and hand them to the task backend."""
    from ..models import WebhookDelivery, WebhookEndpoint
    from ..tasks.backends import enqueue

    if not settings.WEBHOOKS_ENABLED or not event_allowed(event):
        return []
    deliveries = []
    clean = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))
    for endpoint in WebhookEndpoint.objects.filter(is_active=True):
        if not endpoint.wants(event):
            continue
        delivery = WebhookDelivery.objects.create(endpoint=endpoint, event=event, payload=clean)
        deliveries.append(delivery)
        enqueue("fingerprint_attendance.tasks.jobs.deliver_webhook", delivery.pk)
    return deliveries


def deliver(delivery_id: int) -> bool:
    from ..models import WebhookDelivery

    delivery = WebhookDelivery.objects.select_related("endpoint").filter(pk=delivery_id).first()
    if delivery is None or delivery.status == DeliveryStatus.SUCCEEDED:
        return False
    endpoint = delivery.endpoint
    body = json.dumps({"event": delivery.event, "delivery_id": str(delivery.uuid),
                       "created_at": delivery.created_at.isoformat(),
                       "data": delivery.payload}, cls=DjangoJSONEncoder).encode("utf-8")
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": "django-fingerprint-attendance",
               "X-FPA-Event": delivery.event, "X-FPA-Delivery": str(delivery.uuid),
               "X-FPA-Timestamp": timestamp, **{str(k): str(v) for k, v in
                                                (endpoint.headers or {}).items()}}
    secret = endpoint.secret or settings.WEBHOOK_SIGNING_SECRET
    if secret:
        headers["X-FPA-Signature"] = sign(secret, timestamp, body)
    delivery.attempts += 1
    sender = settings.import_("WEBHOOK_SENDER")
    try:
        status, text = sender(endpoint.url, body, headers, settings.WEBHOOK_TIMEOUT)
        delivery.response_status = status
        delivery.response_body = str(text)[:RESPONSE_LIMIT]
        ok = 200 <= status < 300
        delivery.error = "" if ok else f"HTTP {status}"
    except Exception as exc:
        ok = False
        delivery.error = f"{type(exc).__name__}: {exc}"[:1000]
    if ok:
        delivery.status = DeliveryStatus.SUCCEEDED
        delivery.delivered_at = now()
        delivery.next_attempt_at = None
    elif delivery.attempts <= settings.WEBHOOK_MAX_RETRIES:
        delivery.status = DeliveryStatus.RETRYING
        delay = settings.WEBHOOK_RETRY_BACKOFF * (2 ** (delivery.attempts - 1))
        delivery.next_attempt_at = now() + min(delay, timedelta(hours=6))
    else:
        delivery.status = DeliveryStatus.FAILED
        delivery.next_attempt_at = None
        logger.warning("Webhook delivery %s to %s failed permanently: %s", delivery.uuid,
                       endpoint.name, delivery.error)
    delivery.save()
    return ok


def retry_due() -> int:
    from ..models import WebhookDelivery

    due = list(WebhookDelivery.objects.filter(status=DeliveryStatus.RETRYING,
                                              next_attempt_at__lte=now())
               .values_list("pk", flat=True)[:500])
    for delivery_id in due:
        deliver(delivery_id)
    return len(due)


def send_test(endpoint: Any) -> Any:
    """Create and deliver a ``webhook.test`` event immediately (ignores the allowlist)."""
    from ..models import WebhookDelivery

    delivery = WebhookDelivery.objects.create(endpoint=endpoint, event="webhook.test",
                                              payload={"message": "test delivery"})
    deliver(delivery.pk)
    delivery.refresh_from_db()
    return delivery
