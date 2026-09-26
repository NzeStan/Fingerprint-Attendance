# Signals, hooks, webhooks and realtime

`fingerprint_attendance.events.emit()` sends each event, in order, to:

1. the Django signal of the same name (errors are logged, never raised into ingestion),
2. `HOOKS`,
3. webhooks,
4. the realtime backend.

With `EVENTS_ON_COMMIT` (default) this happens after the transaction commits.

Every receiver gets `payload` (JSON-safe, never template bytes or secrets), `event`, and the
model instances listed.

| Event | Extra kwargs |
|---|---|
| `device_registered`, `device_approved`, `device_disabled`, `device_online`, `device_offline`, `device_handshake` | `device` |
| `device_clock_drift` | `device`, `drift_seconds` |
| `device_log_capacity_warning` | `device`, `used`, `capacity` |
| `command_queued`, `command_sent`, `command_succeeded`, `command_failed`, `command_expired` | `command` |
| `enrollment_started`, `enrollment_completed`, `enrollment_expired`, `enrollment_cancelled` | `session` |
| `enrollee_activated`, `enrollee_deactivated` | `enrollee` |
| `template_stored` (`created`), `template_updated` | `template` |
| `template_deleted` | `enrollee`, `finger_index`, `algorithm_version` |
| `sync_queued`, `sync_completed` | `device`, `enrollee` |
| `sync_failed` | `device`, `enrollee`, `error` |
| `punch_received` | `punch` |
| `punches_received` | `punches`, `device` |
| `punch_flagged` | `punch`, `flags` |
| `backlog_synced` | `device`, `count`, `start_date`, `end_date` |
| `attendance_computed` | `attendance_day` |
| `calendar_changed` | `start_date`, `end_date`, `enrollee_ids` |
| `consent_given`, `consent_withdrawn` | `consent`, `enrollee` |

Batches larger than `PUNCH_EVENT_BATCH_LIMIT` emit only the batch events
(`punches_received`, `backlog_synced`).

Register your own event names with `events.register_event("my_event")`.

## Hooks

```python
FINGERPRINT_ATTENDANCE["HOOKS"] = {
    "punch_received": ["myapp.hooks.push_to_payroll"],
    "*": ["myapp.hooks.audit_everything"],
}

def push_to_payroll(event: str, payload: dict) -> None: ...
```

With `HOOKS_ASYNC` (default) each hook runs through the task backend. A failing hook is logged
and never affects ingestion. `hooks.register_hook(event, func)` registers hooks in code.

## Webhooks

Enable `WEBHOOKS_ENABLED`, then create endpoints (admin or `POST webhooks/`). Each delivery is
a JSON `POST`:

```json
{"event": "punch_received", "delivery_id": "…", "created_at": "…", "data": {…}}
```

Headers: `X-FPA-Event`, `X-FPA-Delivery`, `X-FPA-Timestamp`, and
`X-FPA-Signature: sha256=HMAC(secret, "<timestamp>.<body>")`. Failed deliveries retry with
exponential backoff (`WEBHOOK_MAX_RETRIES`, `WEBHOOK_RETRY_BACKOFF`), and every attempt is
logged in `webhook-deliveries/`. `WEBHOOK_EVENTS` is a global allowlist, and endpoints can
subscribe to a subset.

Verifying in a receiver:

```python
from fingerprint_attendance.webhooks.delivery import verify_signature

ok = verify_signature(secret, request.headers["X-FPA-Timestamp"], request.body,
                      request.headers["X-FPA-Signature"], tolerance_seconds=300)
```

## Realtime (Channels)

```python
FINGERPRINT_ATTENDANCE["REALTIME_BACKEND"] = "channels"
# asgi.py
from fingerprint_attendance.realtime.routing import websocket_urlpatterns
application = ProtocolTypeRouter({
    "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    ...
})
```

Clients connect to `/ws/fingerprint-attendance/events/` and receive
`{"event": ..., "payload": ...}` for `REALTIME_EVENTS`. To narrow, send
`{"subscribe": ["fpa.device.<serial>"]}`. Group names come from
`REALTIME_GROUP_NAME_BUILDER`, and access from `REALTIME_CONSUMER_PERMISSION` (staff by default).

## Task backends

`TASK_BACKEND`: `sync` (inline), `celery` (`fingerprint_attendance.run` task, sent after
commit), `django` (Django 6 `django.tasks`), or a class with
`enqueue(func_path, *args, **kwargs)`. Arguments are always JSON-serializable.
`tasks.celery.beat_schedule()` returns the periodic jobs.
