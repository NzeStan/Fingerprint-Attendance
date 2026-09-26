"""Template sync engine: fan-out, compatibility, scope, deletion, backfill, idempotency."""

from __future__ import annotations

import pytest

from fingerprint_attendance import services
from fingerprint_attendance.constants import CommandStatus, SyncStatus
from fingerprint_attendance.models import Device, DeviceCommand, DeviceEnrolleeSync, DeviceGroup
from fingerprint_attendance.sync.engine import (
    choose_templates,
    compatible_algorithms,
    is_compatible,
    sync_status_for_device,
    sync_status_for_enrollee,
)

pytestmark = pytest.mark.django_db


def cmds(device=None, **filters):
    qs = DeviceCommand.objects.filter(**filters)
    if device is not None:
        qs = qs.filter(device=device)
    return list(qs.order_by("id").values_list("command_type", flat=True))


def test_fan_out_to_all_devices(make_enrollee, make_device):
    d1, d2 = make_device(), make_device()
    enrollee = make_enrollee()
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    assert cmds(d1) == ["add_user", "add_template"]
    assert cmds(d2) == ["add_user", "add_template"]
    assert DeviceEnrolleeSync.objects.get(device=d1).status == SyncStatus.PENDING


def test_idempotent_sync(make_enrollee, make_device):
    device = make_device()
    enrollee = make_enrollee()
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    services.sync_enrollee(enrollee)
    services.sync_enrollee(enrollee)
    assert cmds(device, status=CommandStatus.PENDING) == ["add_user", "add_template"]


def test_updated_template_supersedes_pending(make_enrollee, make_device):
    device = make_device()
    enrollee = make_enrollee()
    services.store_template(enrollee, 1, "10", b"old" * 5, source="import")
    services.store_template(enrollee, 1, "10", b"new" * 5, source="import")
    pending = DeviceCommand.objects.filter(device=device, command_type="add_template",
                                           status=CommandStatus.PENDING)
    assert pending.count() == 1 and pending.get().payload["version"] == 2
    assert DeviceCommand.objects.filter(command_type="add_template", status="cancelled").count() \
        == 1


def test_algorithm_compatibility(make_enrollee, make_device, fpa):
    v10, v12, unknown = make_device(algorithm="10"), make_device(algorithm="12"), \
        make_device(algorithm="")
    enrollee = make_enrollee()
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    assert "add_template" in cmds(v10)
    assert cmds(v12) == []
    assert DeviceEnrolleeSync.objects.get(device=v12).status == SyncStatus.INCOMPATIBLE
    assert "add_template" in cmds(unknown)
    assert compatible_algorithms(unknown) is None
    fpa(SYNC_TO_UNKNOWN_ALGORITHM_DEVICES=False)
    assert not is_compatible(unknown, "10")
    fpa(ALGORITHM_COMPATIBILITY_MAP={"12": ["10"]})
    assert is_compatible(v12, "10") and compatible_algorithms(v12) == {"12", "10"}
    services.sync_enrollee(enrollee)
    assert "add_template" in cmds(v12)


def test_choose_prefers_device_algorithm(make_enrollee, make_device, fpa):
    fpa(ALGORITHM_COMPATIBILITY_MAP={"12": ["10"]})
    enrollee = make_enrollee()
    services.store_template(enrollee, 1, "10", b"a" * 10, source="import")
    services.store_template(enrollee, 1, "12", b"b" * 10, source="import")
    v12 = make_device(algorithm="12")
    chosen = choose_templates(v12, list(enrollee.templates.all()))
    assert [t.algorithm_version for t in chosen] == ["12"]


def test_group_strategy(make_enrollee, make_device, fpa):
    fpa(SYNC_STRATEGY="groups")
    hq, branch = DeviceGroup.objects.create(name="HQ"), DeviceGroup.objects.create(name="BR")
    d_hq, d_br = make_device(), make_device()
    d_hq.groups.add(hq)
    d_br.groups.add(branch)
    enrollee = make_enrollee(groups=[hq])
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    assert cmds(d_hq) == ["add_user", "add_template"] and cmds(d_br) == []
    loner = make_enrollee()
    services.store_template(loner, 1, "10", b"y" * 10, source="import")
    assert cmds(d_br) == []
    fpa(SYNC_STRATEGY="groups", SYNC_UNGROUPED_TO_ALL=True)
    services.sync_enrollee(loner)
    assert "add_template" in cmds(d_br)


def test_group_change_moves_enrollee(simulator, make_enrollee, fpa):
    fpa(SYNC_STRATEGY="groups")
    hq, branch = DeviceGroup.objects.create(name="HQ"), DeviceGroup.objects.create(name="BR")
    sim_hq, sim_br = simulator(), simulator()
    Device.objects.get(serial_number=sim_hq.serial).groups.add(hq)
    Device.objects.get(serial_number=sim_br.serial).groups.add(branch)
    enrollee = make_enrollee(pin="10", groups=[hq])
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    sim_hq.run_commands()
    assert "10" in sim_hq.users
    services.update_enrollee(enrollee, groups=[branch])
    sim_hq.run_commands()
    sim_br.run_commands()
    assert "10" not in sim_hq.users and "10" in sim_br.users
    record = DeviceEnrolleeSync.objects.get(device__serial_number=sim_hq.serial)
    assert record.status == SyncStatus.OUT_OF_SCOPE


def test_callable_strategy(make_enrollee, make_device, fpa):
    fpa(SYNC_STRATEGY="tests.testapp.hooks.only_even_devices")
    odd, even = make_device("DEV1"), make_device("DEV2")
    enrollee = make_enrollee()
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    assert cmds(odd) == [] and "add_template" in cmds(even)
    from fingerprint_attendance.sync.strategies import get_sync_strategy

    assert list(get_sync_strategy().enrollees_for(even)) == [enrollee]


def test_deactivation_propagates_and_reactivation_restores(simulator, make_enrollee):
    sim = simulator()
    enrollee = make_enrollee(pin="20")
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    sim.run_commands()
    assert ("20", 1) in sim.templates
    services.deactivate_enrollee(enrollee)
    sim.run_commands()
    assert "20" not in sim.users and sim.templates == {}
    record = DeviceEnrolleeSync.objects.get(enrollee=enrollee)
    assert record.status == SyncStatus.DELETED and not record.user_synced
    services.activate_enrollee(enrollee)
    sim.run_commands()
    assert ("20", 1) in sim.templates


def test_template_delete_propagates(simulator, make_enrollee):
    sim = simulator()
    enrollee = make_enrollee(pin="21")
    t1, _ = services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    services.store_template(enrollee, 2, "10", b"y" * 10, source="import")
    sim.run_commands()
    services.delete_template(t1)
    sim.run_commands()
    assert ("21", 1) not in sim.templates and ("21", 2) in sim.templates
    record = DeviceEnrolleeSync.objects.get(enrollee=enrollee)
    assert set(record.synced_templates) == {"_user", "2:10"}
    assert record.status == SyncStatus.SYNCED


def test_enrollee_delete_propagates_everywhere(simulator, make_enrollee, make_device):
    sim = simulator()
    enrollee = make_enrollee(pin="22")
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    sim.run_commands()
    extra = make_device()  # active device without a sync record
    services.delete_enrollee(enrollee)
    sim.run_commands()
    assert "22" not in sim.users
    assert DeviceCommand.objects.filter(device=extra, command_type="delete_user").exists()


def test_employee_cascade_delete_also_propagates(simulator, make_enrollee):
    sim = simulator()
    enrollee = make_enrollee(pin="23")
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    sim.run_commands()
    enrollee.employee.delete()
    sim.run_commands()
    assert "23" not in sim.users


def test_device_approval_backfills(make_enrollee, simulator):
    enrollee = make_enrollee(pin="30")
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    sim = simulator(approve=False)
    assert sim.poll() == []
    services.approve_device(Device.objects.get(serial_number=sim.serial))
    sim.run_commands()
    assert ("30", 1) in sim.templates


def test_full_resync_pushes_everything(simulator, make_enrollee):
    sim = simulator()
    enrollee = make_enrollee(pin="31")
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    sim.run_commands()
    sim.users.clear()
    sim.templates.clear()  # device lost its data
    device = Device.objects.get(serial_number=sim.serial)
    assert services.resync_device(device)["queued"] == 0  # believes it is in sync
    assert services.resync_device(device, full=True)["queued"] == 2
    sim.run_commands()
    assert ("31", 1) in sim.templates
    assert services.resync_enrollee(enrollee, full=True)["queued"] == 2
    assert services.resync_all()["devices"] == 1


def test_failed_sync_command_marks_failed(simulator, make_enrollee, fpa):
    from fingerprint_attendance.signals import sync_failed

    fpa(COMMAND_MAX_RETRIES=0)
    sim = simulator()
    sim.fail_commands = {"DATA": -1}
    enrollee = make_enrollee(pin="32")
    failures = []
    sync_failed.connect(lambda **kw: failures.append(kw["error"]), weak=False,
                        dispatch_uid="t-sync-failed")
    try:
        services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
        sim.run_commands()
    finally:
        sync_failed.disconnect(dispatch_uid="t-sync-failed")
    record = DeviceEnrolleeSync.objects.get(enrollee=enrollee)
    assert record.status == SyncStatus.FAILED and "return code -1" in record.last_error
    assert failures


def test_user_rename_repushes_user(simulator, make_enrollee):
    sim = simulator()
    enrollee = make_enrollee(pin="33")
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    sim.run_commands()
    services.update_enrollee(enrollee, display_name="New Name")
    sim.run_commands()
    assert sim.users["33"]["name"] == "New Name"


def test_pin_change_moves_user(simulator, make_enrollee):
    sim = simulator()
    enrollee = make_enrollee(pin="34")
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    sim.run_commands()
    services.update_enrollee(enrollee, device_pin="340")
    sim.run_commands()
    assert "34" not in sim.users and "340" in sim.users and ("340", 1) in sim.templates


def test_sync_status_reports(simulator, make_enrollee):
    sim = simulator()
    enrollee = make_enrollee(pin="35")
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    device = Device.objects.get(serial_number=sim.serial)
    report = sync_status_for_device(device)
    assert report["enrollees_in_scope"] == 1 and report["missing"] == 1
    sim.run_commands()
    report = sync_status_for_device(device)
    assert report["synced"] == 1 and report["missing"] == 0
    [row] = sync_status_for_enrollee(enrollee)
    assert row["status"] == "synced" and row["templates"] == ["1:10"]


def test_users_without_templates_option(make_enrollee, make_device, fpa):
    device = make_device()
    make_enrollee()
    assert cmds(device) == []
    fpa(SYNC_USERS_WITHOUT_TEMPLATES=True)
    make_enrollee()
    assert cmds(device) == ["add_user"]


def test_sync_disabled_on_enroll(make_enrollee, make_device, fpa):
    fpa(SYNC_ON_ENROLL=False, SYNC_ON_DELETE=False)
    device = make_device()
    enrollee = make_enrollee()
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    services.deactivate_enrollee(enrollee)
    assert cmds(device) == []


def test_device_group_change_reconciles_scope(simulator, make_enrollee, fpa):
    fpa(SYNC_STRATEGY="groups")
    hq = DeviceGroup.objects.create(name="HQ")
    sim = simulator()
    device = Device.objects.get(serial_number=sim.serial)
    enrollee = make_enrollee(pin="36", groups=[hq])
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    assert sim.poll() == []
    device.groups.add(hq)
    sim.run_commands()
    assert "36" in sim.users
    device.groups.remove(hq)
    sim.run_commands()
    assert "36" not in sim.users
