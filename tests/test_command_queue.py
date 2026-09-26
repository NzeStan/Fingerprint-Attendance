"""Command queue: FIFO, dedupe/supersede, retries/backoff, expiry, acknowledgements."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from fingerprint_attendance import services
from fingerprint_attendance.adms.adapter import CommandResult
from fingerprint_attendance.constants import CommandStatus
from fingerprint_attendance.exceptions import InvalidInput, NotAllowed
from fingerprint_attendance.models import AuditLog, DeviceCommand, DeviceEventLog

pytestmark = pytest.mark.django_db


def test_fifo_and_limit(make_device, fpa):
    fpa(ADMS_MAX_COMMANDS_PER_REQUEST=2)
    device = make_device()
    ids = [services.queue_command(device, "check", {}).pk for _ in range(3)]
    first = services.fetch_commands(device)
    assert [cid for cid, _ in first] == ids[:2]
    assert [cid for cid, _ in services.fetch_commands(device)] == ids[2:]
    assert services.fetch_commands(device) == []


def test_dedupe_reuses_identical_and_supersedes_changed(make_device):
    device = make_device()
    a = services.queue_command(device, "set_option", {"key": "Delay", "value": 5},
                               dedupe_key="opt:delay")
    b = services.queue_command(device, "set_option", {"key": "Delay", "value": 5},
                               dedupe_key="opt:delay")
    assert a.pk == b.pk
    c = services.queue_command(device, "set_option", {"key": "Delay", "value": 9},
                               dedupe_key="opt:delay")
    a.refresh_from_db()
    assert a.status == CommandStatus.CANCELLED and a.error == "superseded"
    assert c.status == CommandStatus.PENDING


def test_validation(make_device):
    device = make_device()
    with pytest.raises(InvalidInput, match="missing"):
        services.queue_command(device, "delete_user", {})
    from fingerprint_attendance.adms.commands import CommandBuildError

    with pytest.raises(CommandBuildError):
        services.queue_command(device, "nope", {})


def test_dangerous_commands_are_audited(make_device, admin_user):
    device = make_device()
    services.queue_command(device, "reboot", {}, created_by=admin_user)
    entry = AuditLog.objects.get(action="command.reboot")
    assert entry.actor == admin_user and entry.object_id == str(device.uuid)


def test_sensitive_commands_stored_redacted(make_device, make_enrollee):
    from fingerprint_attendance.constants import TemplateSource

    enrollee = make_enrollee()
    template, _ = services.store_template(enrollee, 1, "10", b"Q" * 300,
                                          source=TemplateSource.IMPORT, fan_out=False)
    device = make_device()
    cmd = services.queue_command(device, "add_template",
                                 {"template_id": template.pk, "checksum": template.checksum})
    assert "rendered at send time" in cmd.command_string
    [(_cid, text)] = services.fetch_commands(device)
    assert "TMP=" in text and "UVFR" in text  # base64 of QQQ is UVFR
    cmd.refresh_from_db()
    assert "UVFR" not in cmd.command_string


def test_failure_retries_with_backoff_then_fails(make_device, fpa):
    fpa(COMMAND_MAX_RETRIES=1, COMMAND_RETRY_BACKOFF=60)
    device = make_device()
    cmd = services.queue_command(device, "check", {})
    services.fetch_commands(device)
    services.record_results(device, [CommandResult(cmd.pk, -2, "CHECK")])
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.PENDING and cmd.next_attempt_at > timezone.now()
    assert services.fetch_commands(device) == []  # not due yet
    DeviceCommand.objects.filter(pk=cmd.pk).update(next_attempt_at=timezone.now())
    assert len(services.fetch_commands(device)) == 1
    services.record_results(device, [CommandResult(cmd.pk, -2, "CHECK")])
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.FAILED and cmd.attempts == 2
    assert cmd.return_code == -2


def test_results_are_idempotent_and_unknown_logged(make_device):
    device = make_device()
    cmd = services.queue_command(device, "check", {})
    services.fetch_commands(device)
    assert services.record_results(device, [CommandResult(cmd.pk, 0, "CHECK")]) == 1
    assert services.record_results(device, [CommandResult(cmd.pk, -1, "CHECK")]) == 0
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.SUCCEEDED
    services.record_results(device, [CommandResult(987654, 0, "X")])
    assert DeviceEventLog.objects.filter(message__contains="unknown command").exists()


def test_results_for_other_device_are_ignored(make_device):
    a, b = make_device(), make_device()
    cmd = services.queue_command(a, "check", {})
    services.record_results(b, [CommandResult(cmd.pk, 0, "CHECK")])
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.PENDING


def test_expiry(make_device, fpa):
    device = make_device()
    reboot = services.queue_command(device, "reboot", {})
    add_user = services.queue_command(device, "add_user", {"pin": "1"})
    assert reboot.expires_at is not None and add_user.expires_at is None
    DeviceCommand.objects.filter(pk=reboot.pk).update(expires_at=timezone.now())
    handed = services.fetch_commands(device)
    assert [cid for cid, _ in handed] == [add_user.pk]
    reboot.refresh_from_db()
    assert reboot.status == CommandStatus.EXPIRED
    fpa(COMMAND_EXPIRY={"default": "1m"})
    other = services.queue_command(device, "check", {})
    DeviceCommand.objects.filter(pk=other.pk).update(expires_at=timezone.now())
    assert services.expire_commands() == 1


def test_unacknowledged_commands_retry(make_device, fpa):
    fpa(COMMAND_ACK_TIMEOUT=60)
    device = make_device()
    cmd = services.queue_command(device, "check", {})
    services.fetch_commands(device)
    DeviceCommand.objects.filter(pk=cmd.pk).update(sent_at=timezone.now() - timedelta(hours=1))
    assert services.retry_unacknowledged() == 1
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.PENDING and "acknowledgement" in cmd.error


def test_cancel_and_retry(make_device, admin_user):
    device = make_device()
    cmd = services.queue_command(device, "check", {})
    services.cancel_command(cmd, by=admin_user)
    assert cmd.status == CommandStatus.CANCELLED
    with pytest.raises(NotAllowed):
        services.cancel_command(cmd)
    services.retry_command(cmd, by=admin_user)
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.PENDING and cmd.attempts == 0
    with pytest.raises(NotAllowed):
        services.retry_command(cmd)


def test_build_error_at_send_cancels(make_device, make_enrollee):
    from fingerprint_attendance.constants import TemplateSource

    enrollee = make_enrollee()
    template, _ = services.store_template(enrollee, 1, "10", b"Z" * 50,
                                          source=TemplateSource.IMPORT, fan_out=False)
    device = make_device()
    cmd = services.queue_command(device, "add_template", {"template_id": template.pk})
    template.delete()
    assert services.fetch_commands(device) == []
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.CANCELLED and "no longer exists" in cmd.error


def test_offline_device_gets_commands_in_order_on_reconnect(simulator):
    sim = simulator()
    from fingerprint_attendance.models import Device

    device = Device.objects.get(serial_number=sim.serial)
    sim.go_offline()
    queued = [services.queue_command(device, "add_user", {"pin": str(i), "name": f"U{i}"})
              for i in range(5)]
    sim.go_online()
    applied = sim.run_commands()
    assert [a.split("PIN=")[1].split("\t")[0] for a in applied] == [str(i) for i in range(5)]
    assert set(sim.users) == {"0", "1", "2", "3", "4"}
    assert all(DeviceCommand.objects.get(pk=c.pk).status == "succeeded" for c in queued)
