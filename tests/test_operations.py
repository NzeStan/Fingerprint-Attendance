"""Management commands, retention, audit log, log redaction and the admin."""

from __future__ import annotations

import logging
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.utils import timezone

from fingerprint_attendance import services
from fingerprint_attendance.models import (
    AuditLog,
    Device,
    DeviceCommand,
    DeviceEventLog,
    FingerprintTemplate,
    Punch,
    WebhookDelivery,
    WebhookEndpoint,
)
from fingerprint_attendance.utils.logging import get_logger, redact
from tests.conftest import local

pytestmark = pytest.mark.django_db


def run(*args, **kwargs):
    out = StringIO()
    call_command(*args, stdout=out, stderr=StringIO(), **kwargs)
    return out.getvalue()


def test_maintenance_commands(make_device, make_enrollee):
    device = make_device(last_seen_at=timezone.now() - timedelta(hours=1),
                         connection_state="online")
    assert "1 device(s) marked offline" in run("fpa_check_devices")
    cmd = services.queue_command(device, "reboot", {})
    DeviceCommand.objects.filter(pk=cmd.pk).update(expires_at=timezone.now())
    assert "1 command(s) expired" in run("fpa_expire_commands")
    assert "0 command(s) rescheduled" in run("fpa_retry_commands")
    enrollee = make_enrollee()
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    assert "queued on 1 device(s)" in run("fpa_resync_all", "--full")
    key = run("fpa_generate_key").strip()
    assert len(key) == 44
    assert "key: fpa_" in run("fpa_create_agent", "Desk", "--algorithm", "12")


def test_recompute_and_absence_commands(simulator, make_enrollee):
    enrollee = make_enrollee(pin="5")
    sim = simulator()
    sim.punch("5", local(2026, 1, 5, 8))
    assert "1 attendance day(s)" in run("fpa_recompute_attendance", "--from", "2026-01-01",
                                        "--to", "2026-01-10", "--enrollee", "5")
    run("fpa_recompute_attendance", "--from", "2026-01-01", "--to", "2026-01-10",
        "--enrollee", str(enrollee.uuid))
    with pytest.raises(CommandError, match="unknown enrollee"):
        run("fpa_recompute_attendance", "--from", "2026-01-01", "--to", "2026-01-02",
            "--enrollee", "zzz")
    with pytest.raises(CommandError):
        run("fpa_recompute_attendance", "--from", "2026-01-05", "--to", "2026-01-01")
    with pytest.raises(CommandError):
        run("fpa_recompute_attendance", "--from", "bad", "--to", "2026-01-01")
    assert "generated" in run("fpa_generate_absences", "--date", "2026-01-06", "--force")
    run("fpa_generate_absences")


def test_recompute_disabled(fpa):
    fpa(ATTENDANCE_PROCESSOR=None)
    with pytest.raises(CommandError, match="disabled"):
        run("fpa_recompute_attendance", "--from", "2026-01-01", "--to", "2026-01-02")


def test_reupload_command(simulator):
    sim = simulator()
    sim.punch("1", local(2026, 1, 5, 8))
    out = run("fpa_reupload_device_logs", "--device", sim.serial, "--from", "2026-01-01T00:00")
    assert "cursor reset, 2 command(s)" in out
    assert Device.objects.get(serial_number=sim.serial).attlog_stamp == "0"
    with pytest.raises(CommandError):
        run("fpa_reupload_device_logs", "--device", sim.serial, "--from", "yesterday")


def test_sync_device_command_for_push(simulator):
    sim = simulator()
    assert sim.serial in run("fpa_sync_device", "--device", sim.serial)


def test_retention_purge(make_enrollee, make_device, fpa):
    enrollee = make_enrollee()
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    services.deactivate_enrollee(enrollee)
    device = make_device()
    old = timezone.now() - timedelta(days=400)
    Punch.objects.create(raw_pin="1", device=device, punched_at=old, dedupe_hash="a" * 64,
                         source="manual")
    Punch.objects.create(raw_pin="1", device=device, punched_at=timezone.now(),
                         dedupe_hash="b" * 64, source="manual")
    DeviceEventLog.objects.create(device=device, event_type="other", created_at=old)
    endpoint = WebhookEndpoint.objects.create(name="x", url="https://x.example.com")
    WebhookDelivery.objects.create(endpoint=endpoint, event="e", status="succeeded")
    WebhookDelivery.objects.filter().update(created_at=old)
    done = services.queue_command(device, "check", {})
    DeviceCommand.objects.filter(pk=done.pk).update(status="succeeded")
    DeviceCommand.objects.filter(pk=done.pk).update(updated_at=old)
    fpa(RETENTION_DELETE_TEMPLATES_AFTER_DEACTIVATION="0s", RETENTION_PUNCHES_DAYS=365,
        RETENTION_AUDIT_LOG_DAYS=1)
    AuditLog.objects.update(created_at=old)
    dry = run("fpa_purge_retention", "--dry-run")
    assert "would delete 1 punches" in dry and FingerprintTemplate.objects.exists()
    counts = services.purge_retention()
    assert counts["templates"] == 1 and counts["punches"] == 1 and counts["event_logs"] == 1
    assert counts["webhook_deliveries"] == 1 and counts["commands"] == 1
    assert not FingerprintTemplate.objects.exists() and Punch.objects.count() == 1
    assert AuditLog.objects.filter(action="retention.purge").exists()


def test_audit_trail(make_enrollee, admin_user):
    enrollee = make_enrollee()
    services.withdraw_consent(enrollee, by=admin_user)
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"enrollee.create", "consent.give", "consent.withdraw"} <= actions
    entry = AuditLog.objects.get(action="consent.withdraw")
    assert entry.actor == admin_user and entry.actor_repr == "admin"


def test_audit_can_be_disabled(fpa, make_enrollee):
    fpa(AUDIT_LOG_ENABLED=False)
    make_enrollee()
    assert not AuditLog.objects.exists()


def test_redaction():
    text = redact("FP PIN=1\tTMP=QUJDREVGR0g=\ttoken=abcdef Authorization: Agent fpa_1234567890"
                  + " " + "A" * 200 + ' {"template": "xyz"}')
    assert "QUJDREVGR0g" not in text and "abcdef" not in text and "fpa_1234567890" not in text
    assert "A" * 200 not in text and '"template": "[REDACTED]"' in text
    assert "PIN=1" in text


def test_package_loggers_redact(caplog, fpa):
    logger = get_logger("fingerprint_attendance.test")
    assert get_logger("custom").name == "fingerprint_attendance.custom"
    with caplog.at_level(logging.INFO, logger="fingerprint_attendance"):
        logger.info("upload %s", "TMP=SECRETSECRET")
    assert "SECRETSECRET" not in caplog.text
    fpa(LOG_REDACTION=False)
    with caplog.at_level(logging.INFO, logger="fingerprint_attendance"):
        logger.info("raw %s", "TMP=VISIBLE")
    assert "VISIBLE" in caplog.text


def test_admin_pages_render(admin_user, simulator, make_enrollee):
    enrollee = make_enrollee(pin="9")
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    sim = simulator()
    sim.punch("9", local(2026, 1, 5, 8))
    client = Client()
    client.force_login(admin_user)
    for name in ("device", "enrollee", "fingerprinttemplate", "punch", "devicecommand",
                 "attendanceday", "auditlog", "deviceeventlog", "holiday", "consentrecord",
                 "enrollmentagent", "enrollmentsession", "deviceenrolleesync",
                 "webhookendpoint", "webhookdelivery", "devicegroup", "punchadjustment"):
        response = client.get(f"/admin/fingerprint_attendance/{name}/")
        assert response.status_code == 200, name
    template = FingerprintTemplate.objects.get()
    page = client.get(f"/admin/fingerprint_attendance/fingerprinttemplate/{template.pk}/change/")
    assert page.status_code == 200 and "fernet:" not in page.content.decode()
    assert client.get("/admin/fingerprint_attendance/punch/add/").status_code == 403


def test_admin_actions(admin_user, make_device, make_enrollee):
    device = make_device(status="pending_approval")
    enrollee = make_enrollee()
    client = Client()
    client.force_login(admin_user)
    url = "/admin/fingerprint_attendance/device/"
    for action in ("approve", "resync", "full_resync", "request_info", "sync_time",
                   "force_reupload", "disable"):
        response = client.post(url, {"action": action, "_selected_action": [device.pk]})
        assert response.status_code == 302, action
    device.refresh_from_db()
    assert device.status == "disabled"
    url = "/admin/fingerprint_attendance/enrollee/"
    for action in ("deactivate", "activate", "resync"):
        client.post(url, {"action": action, "_selected_action": [enrollee.pk]})
    cmd = DeviceCommand.objects.first()
    url = "/admin/fingerprint_attendance/devicecommand/"
    client.post(url, {"action": "cancel", "_selected_action": [cmd.pk]})
    client.post(url, {"action": "cancel", "_selected_action": [cmd.pk]})  # error path
    client.post(url, {"action": "retry", "_selected_action": [cmd.pk]})
    response = client.post("/admin/fingerprint_attendance/enrollmentagent/add/",
                           {"name": "Desk", "algorithm_version": "10", "is_active": "on",
                            "metadata": "{}"})
    assert response.status_code == 302
    assert AuditLog.objects.filter(action="agent.create").exists()
    response = client.post(f"/admin/fingerprint_attendance/enrollee/{enrollee.pk}/delete/",
                           {"post": "yes"})
    assert response.status_code == 302
