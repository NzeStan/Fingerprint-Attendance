"""Signals, hooks, webhooks, task backends and realtime broadcasting."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.utils import timezone

from fingerprint_attendance import services
from fingerprint_attendance.events import EVENTS, after_commit, emit, register_event
from fingerprint_attendance.hooks import hooks_for, register_hook, unregister_hook
from fingerprint_attendance.models import WebhookDelivery, WebhookEndpoint
from fingerprint_attendance.tasks.backends import (
    DjangoTaskBackend,
    SyncTaskBackend,
    enqueue,
    run_task,
)
from fingerprint_attendance.webhooks.delivery import sign, verify_signature
from tests.conftest import local
from tests.testapp import hooks as test_hooks

pytestmark = pytest.mark.django_db

SENT: list[dict[str, Any]] = []


def fake_sender(url: str, body: bytes, headers: dict[str, str], timeout: int) -> tuple[int,
                                                                                         str]:
    SENT.append({"url": url, "body": body, "headers": headers})
    if "fail" in url:
        return 500, "nope"
    if "boom" in url:
        raise OSError("connection refused")
    return 200, "ok"


@pytest.fixture(autouse=True)
def _reset():
    SENT.clear()
    test_hooks.CALLS.clear()


def test_every_required_signal_exists():
    required = {
        "device_registered", "device_approved", "device_online", "device_offline",
        "device_handshake", "command_queued", "command_sent", "command_succeeded",
        "command_failed", "enrollment_started", "enrollment_completed", "enrollment_expired",
        "template_stored", "template_updated", "template_deleted", "sync_completed",
        "sync_failed", "punch_received", "punches_received", "punch_flagged",
        "attendance_computed", "consent_given", "consent_withdrawn", "backlog_synced",
        "calendar_changed", "device_clock_drift", "device_log_capacity_warning",
    }
    assert required <= set(EVENTS)


def test_unknown_event_and_registration():
    with pytest.raises(KeyError):
        emit("nope", {})
    register_event("custom_event")
    emit("custom_event", {"a": 1})
    EVENTS.pop("custom_event")


def test_hooks_from_settings_run_isolated(simulator, fpa):
    fpa(HOOKS={"punch_received": ["tests.testapp.hooks.explode",
                                  "tests.testapp.hooks.record"],
               "*": "tests.testapp.hooks.record"})
    sim = simulator()
    sim.punch("1", local(2026, 1, 5, 8))  # ingestion must survive the exploding hook
    events = [e for e, _ in test_hooks.CALLS]
    assert events.count("punch_received") == 2  # explicit + wildcard
    payload = dict(test_hooks.CALLS)["punch_received"]
    assert payload["raw_pin"] == "1" and "template" not in json.dumps(payload)


def test_runtime_hook_registration():
    calls = []
    func = lambda event, payload: calls.append(event)  # noqa: E731
    register_hook("device_online", func)
    assert func in hooks_for("device_online")
    emit("device_online", {})
    unregister_hook("device_online", func)
    emit("device_online", {})
    assert calls == ["device_online"]


def test_hooks_async_go_through_task_backend(fpa):
    fpa(HOOKS={"device_online": ["tests.testapp.hooks.record"]}, HOOKS_ASYNC=True)
    with mock.patch("fingerprint_attendance.tasks.backends.SyncTaskBackend.enqueue") as enq:
        emit("device_online", {"x": 1})
    enq.assert_called_once_with("fingerprint_attendance.hooks.call_hook",
                                "tests.testapp.hooks.record", "device_online", {"x": 1})
    fpa(HOOKS={"device_online": ["tests.testapp.hooks.record"]}, HOOKS_ASYNC=False)
    emit("device_online", {"x": 2})
    assert test_hooks.CALLS == [("device_online", {"x": 2})]


def test_signal_receiver_errors_are_isolated(simulator):
    from fingerprint_attendance.signals import punch_received

    def broken(**kwargs):
        raise ValueError("receiver bug")

    punch_received.connect(broken, weak=False, dispatch_uid="t-broken")
    try:
        sim = simulator()
        sim.punch("1", local(2026, 1, 5, 8))
    finally:
        punch_received.disconnect(dispatch_uid="t-broken")
    from fingerprint_attendance.models import Punch

    assert Punch.objects.count() == 1


def test_events_wait_for_commit(fpa, django_capture_on_commit_callbacks):
    fpa(EVENTS_ON_COMMIT=True, HOOKS={"device_online": ["tests.testapp.hooks.record"]},
        HOOKS_ASYNC=False)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        emit("device_online", {"x": 1})
        seen = []
        after_commit(lambda: seen.append(1))
    assert test_hooks.CALLS == [] and seen == []
    for callback in callbacks:
        callback()
    assert test_hooks.CALLS == [("device_online", {"x": 1})] and seen == [1]


# --------------------------------------------------------------------------- webhooks


def test_webhook_delivery_signed(simulator, fpa):
    fpa(WEBHOOKS_ENABLED=True, WEBHOOK_SIGNING_SECRET="global",
        WEBHOOK_SENDER="tests.test_events.fake_sender")
    WebhookEndpoint.objects.create(name="all", url="https://a.example.com/")
    WebhookEndpoint.objects.create(name="punches", url="https://b.example.com/",
                                   events=["punch_received"], secret="own",
                                   headers={"X-Tenant": "7"})
    WebhookEndpoint.objects.create(name="off", url="https://c.example.com/", is_active=False)
    sim = simulator()
    sim.punch("1", local(2026, 1, 5, 8))
    b_calls = [s for s in SENT if s["url"].startswith("https://b.")]
    assert len(b_calls) == 1
    call = b_calls[0]
    headers = call["headers"]
    assert headers["X-FPA-Event"] == "punch_received" and headers["X-Tenant"] == "7"
    assert verify_signature("own", headers["X-FPA-Timestamp"], call["body"],
                            headers["X-FPA-Signature"])
    body = json.loads(call["body"])
    assert body["event"] == "punch_received" and body["data"]["raw_pin"] == "1"
    assert not any(s["url"].startswith("https://c.") for s in SENT)
    assert WebhookDelivery.objects.filter(status="succeeded").count() == len(SENT)


def test_webhook_allowlist(fpa):
    fpa(WEBHOOKS_ENABLED=True, WEBHOOK_EVENTS=["device_offline"],
        WEBHOOK_SENDER="tests.test_events.fake_sender")
    WebhookEndpoint.objects.create(name="all", url="https://a.example.com/")
    emit("device_online", {})
    emit("device_offline", {})
    assert [s["headers"]["X-FPA-Event"] for s in SENT] == ["device_offline"]


def test_webhook_retries_with_backoff(fpa):
    fpa(WEBHOOKS_ENABLED=True, WEBHOOK_MAX_RETRIES=2, WEBHOOK_RETRY_BACKOFF=10,
        WEBHOOK_SENDER="tests.test_events.fake_sender")
    WebhookEndpoint.objects.create(name="bad", url="https://fail.example.com/")
    WebhookEndpoint.objects.create(name="down", url="https://boom.example.com/")
    emit("device_offline", {})
    deliveries = {d.endpoint.name: d for d in WebhookDelivery.objects.all()}
    assert deliveries["bad"].status == "retrying" and deliveries["bad"].response_status == 500
    assert "connection refused" in deliveries["down"].error
    WebhookDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    from fingerprint_attendance.tasks.jobs import retry_webhook_deliveries

    assert retry_webhook_deliveries() == 2
    WebhookDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    retry_webhook_deliveries()
    assert set(WebhookDelivery.objects.values_list("status", flat=True)) == {"failed"}
    fpa(WEBHOOKS_ENABLED=False)
    assert retry_webhook_deliveries() == 0


def test_signature_helpers():
    sig = sign("k", "100", b"{}")
    assert sig.startswith("sha256=")
    assert not verify_signature("k", "100", b"{}", sig)  # too old
    assert not verify_signature("k", "abc", b"{}", sig)


def test_urllib_sender(monkeypatch):
    from fingerprint_attendance.webhooks import delivery

    class Resp:
        status = 204

        def read(self, n):
            return b"done"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(delivery.urllib.request, "urlopen", lambda req, timeout: Resp())
    assert delivery.urllib_sender("https://x", b"{}", {}, 5) == (204, "done")

    def raise_http(req, timeout):
        import io
        import urllib.error

        raise urllib.error.HTTPError("https://x", 410, "gone", {}, io.BytesIO(b"gone"))

    monkeypatch.setattr(delivery.urllib.request, "urlopen", raise_http)
    assert delivery.urllib_sender("https://x", b"{}", {}, 5) == (410, "gone")


# --------------------------------------------------------------------------- task backends


def test_sync_backend_error_handling():
    backend = SyncTaskBackend()
    assert backend.enqueue("tests.testapp.hooks.explode", "e", {}) is None  # swallowed
    with pytest.raises(RuntimeError):
        SyncTaskBackend(raise_errors=True).enqueue("tests.testapp.hooks.explode", "e", {})
    assert run_task("tests.testapp.hooks.shout_name",
                    [type("E", (), {"full_name": "x"})()]) == "X"


def test_celery_backend(fpa, django_capture_on_commit_callbacks):
    fpa(TASK_BACKEND="celery", TASK_BACKEND_OPTIONS={"queue": "fpa"})
    with mock.patch("fingerprint_attendance.tasks.celery.run.apply_async") as apply_async, \
            django_capture_on_commit_callbacks(execute=True):
        enqueue("fingerprint_attendance.tasks.jobs.check_devices")
    apply_async.assert_called_once_with(
        args=["fingerprint_attendance.tasks.jobs.check_devices", [], {}], queue="fpa")
    from fingerprint_attendance.tasks.celery import beat_schedule, run

    assert run("fingerprint_attendance.tasks.jobs.check_devices") == 0
    schedule = beat_schedule({"fingerprint_attendance.tasks.jobs.purge_retention": None},
                             queue="q")
    assert "fpa:check_devices" in schedule and "fpa:purge_retention" not in schedule
    assert schedule["fpa:check_devices"]["options"] == {"queue": "q"}


@pytest.mark.skipif(__import__("importlib").util.find_spec("django.tasks") is None,
                    reason="django.tasks requires Django 6.0+")
def test_django_tasks_backend(fpa, settings, django_capture_on_commit_callbacks):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    fpa(TASK_BACKEND="django", TASK_BACKEND_OPTIONS={"queue_name": "default"})
    assert isinstance(__import__("fingerprint_attendance.tasks.backends",
                                 fromlist=["get_task_backend"]).get_task_backend(),
                      DjangoTaskBackend)
    with mock.patch("fingerprint_attendance.tasks.jobs.check_devices", return_value=0) as job, \
            django_capture_on_commit_callbacks(execute=True):
        enqueue("fingerprint_attendance.tasks.jobs.check_devices")
    job.assert_called_once()


def test_custom_backend_instance(fpa):
    calls = []

    class Recorder:
        def enqueue(self, func_path, *args, **kwargs):
            calls.append(func_path)

    fpa(TASK_BACKEND=Recorder())
    enqueue("x.y")
    assert calls == ["x.y"]


# --------------------------------------------------------------------------- realtime


def test_channels_broadcast(simulator, fpa, settings):
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    fpa(REALTIME_BACKEND="channels")
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    async_to_sync(layer.group_add)("fpa.all", "test-channel")
    sim = simulator()
    sim.punch("1", local(2026, 1, 5, 8))
    import asyncio

    async def drain():
        out = []
        while len(out) < 20:
            try:
                out.append(await asyncio.wait_for(layer.receive("test-channel"), 0.2))
            except asyncio.TimeoutError:
                break
        return out

    messages = async_to_sync(drain)()
    events = [m["event"] for m in messages]
    assert "punch_received" in events and "device_online" in events


def test_group_names_and_permissions():
    from fingerprint_attendance.realtime.backends import (
        NullRealtimeBackend,
        default_group_names,
        staff_only,
    )

    names = default_group_names("punch_received", {"device_serial": "AB 1"})
    assert names == ["fpa.all", "fpa.event.punch_received", "fpa.device.AB_1"]
    assert default_group_names("backlog_synced", {"device": {"serial_number": "Z"}})[-1] == \
        "fpa.device.Z"
    assert NullRealtimeBackend().broadcast("x", {}) is None
    user = type("U", (), {"is_authenticated": True, "is_staff": True})()
    assert staff_only({"user": user}) and not staff_only({})


def test_channels_without_layer_logs(fpa, settings, caplog):
    settings.CHANNEL_LAYERS = {}
    fpa(REALTIME_BACKEND="channels")
    from fingerprint_attendance.realtime.backends import ChannelsRealtimeBackend

    with mock.patch("channels.layers.get_channel_layer", return_value=None):
        ChannelsRealtimeBackend().broadcast("device_online", {})


def test_consumer(settings, fpa):
    pytest.importorskip("daphne")  # channels.testing depends on it
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from channels.testing import WebsocketCommunicator

    from fingerprint_attendance.realtime.consumers import EventsConsumer

    async def scenario(user):
        communicator = WebsocketCommunicator(EventsConsumer.as_asgi(), "/ws/")
        communicator.scope["user"] = user
        connected, _ = await communicator.connect()
        if not connected:
            return False, None
        await communicator.send_json_to({"subscribe": ["fpa.device.X", "evil"]})
        reply = await communicator.receive_json_from()
        layer = get_channel_layer()
        await layer.group_send("fpa.device.X", {"type": "fpa.event", "event": "punch_received",
                                                "payload": {"a": 1}})
        message = await communicator.receive_json_from()
        await communicator.disconnect()
        return reply, message

    staff = type("U", (), {"is_authenticated": True, "is_staff": True})()
    reply, message = async_to_sync(scenario)(staff)
    assert reply == {"subscribed": ["fpa.device.X"]}
    assert message == {"event": "punch_received", "payload": {"a": 1}}
    anonymous = type("U", (), {"is_authenticated": False, "is_staff": False})()
    assert async_to_sync(scenario)(anonymous) == (False, None)


def test_routing_module():
    from fingerprint_attendance.realtime.routing import websocket_urlpatterns

    assert websocket_urlpatterns[0].pattern.describe().startswith("'ws/")


def test_service_send_command_alias(make_device):
    cmd = services.send_command(make_device(), "check")
    assert cmd.command_type == "check"
