"""Enrollment: walk-up, remote-triggered, desktop agent; consent and finger rules."""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from django.utils import timezone

from fingerprint_attendance import services
from fingerprint_attendance.constants import ConsentState, SessionStatus, SyncStatus
from fingerprint_attendance.exceptions import (
    AlgorithmIncompatible,
    ConsentRequired,
    InvalidInput,
    NotAllowed,
    SessionClosed,
)
from fingerprint_attendance.models import (
    Device,
    DeviceCommand,
    DeviceEnrolleeSync,
    DeviceEventLog,
    EnrollmentSession,
    FingerprintTemplate,
    RetiredPin,
)

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- enrollees


def test_create_enrollee_generates_sequential_pin(make_enrollee):
    a, b = make_enrollee(consent=False), make_enrollee(consent=False)
    assert (a.device_pin, b.device_pin) == ("1", "2")
    assert a.display_name.startswith("Employee")


def test_pin_validation_and_retirement(make_employee, make_enrollee, fpa):
    with pytest.raises(InvalidInput, match="numeric"):
        services.create_enrollee(make_employee(), device_pin="A1")
    with pytest.raises(InvalidInput, match="longer"):
        services.create_enrollee(make_employee(), device_pin="1" * 10)
    enrollee = make_enrollee(pin="77", consent=False)
    with pytest.raises(InvalidInput, match="in use"):
        services.create_enrollee(make_employee(), device_pin="77")
    with pytest.raises(InvalidInput, match="already enrolled"):
        services.create_enrollee(enrollee.employee)
    services.delete_enrollee(enrollee)
    assert RetiredPin.objects.filter(pin="77").exists()
    with pytest.raises(InvalidInput, match="cannot be reused"):
        services.create_enrollee(make_employee(), device_pin="77")
    assert services.create_enrollee(make_employee()).device_pin == "78"
    fpa(PIN_REUSE_ALLOWED=True)
    assert services.create_enrollee(make_employee(), device_pin="77").device_pin == "77"


def test_employee_field_pin_generator(make_employee, fpa):
    fpa(PIN_GENERATOR="employee_field", PIN_SOURCE_FIELD="staff_number", PIN_NUMERIC_ONLY=False)
    enrollee = services.create_enrollee(make_employee(staff_number="E42"))
    assert enrollee.device_pin == "E42"


def test_display_name_callable(make_employee, fpa):
    fpa(EMPLOYEE_DISPLAY_FIELD="tests.testapp.hooks.shout_name")
    enrollee = services.create_enrollee(make_employee(name="ada"))
    assert enrollee.display_name == "ADA"


def test_unknown_punches_linked_on_enroll(make_employee, simulator):
    sim = simulator()
    from tests.conftest import local

    sim.punch("500", local(2026, 1, 5, 8))
    enrollee = services.create_enrollee(make_employee(), device_pin="500")
    assert enrollee.punches.count() == 1


# --------------------------------------------------------------------------- consent


def test_consent_required_for_templates(make_enrollee):
    enrollee = make_enrollee(consent=False)
    with pytest.raises(ConsentRequired):
        services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    services.give_consent(enrollee, version="v2")
    assert services.store_template(enrollee, 1, "10", b"x" * 10, source="import")[1]


def test_consent_withdrawal_deletes_everywhere(make_enrollee, make_device, admin_user):
    enrollee = make_enrollee()
    d1, d2 = make_device(), make_device()
    services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    assert DeviceCommand.objects.filter(command_type="add_template").count() == 2
    records = services.withdraw_consent(enrollee, by=admin_user, reason="asked")
    enrollee.refresh_from_db()
    assert enrollee.consent_state == ConsentState.WITHDRAWN
    assert records[0].withdrawn_at is not None
    assert not FingerprintTemplate.objects.exists()
    deletes = DeviceCommand.objects.filter(command_type="delete_user", status="pending")
    assert {c.device_id for c in deletes} == {d1.pk, d2.pk}
    # the pending adds were superseded
    assert not DeviceCommand.objects.filter(command_type="add_template",
                                            status="pending").exists()
    with pytest.raises(NotAllowed):
        services.withdraw_consent(enrollee)


# --------------------------------------------------------------------------- templates


def test_finger_rules(make_enrollee, fpa):
    fpa(ALLOWED_FINGER_INDEXES=[5, 6], FINGERS_ALLOWED_MAX=1)
    enrollee = make_enrollee()
    with pytest.raises(InvalidInput, match="not allowed"):
        services.store_template(enrollee, 1, "10", b"x", source="import")
    services.store_template(enrollee, 5, "10", b"x", source="import")
    with pytest.raises(InvalidInput, match="at most 1"):
        services.store_template(enrollee, 6, "10", b"y", source="import")
    services.store_template(enrollee, 5, "10", b"z", source="import")  # re-enroll same finger
    with pytest.raises(InvalidInput, match="empty"):
        services.store_template(enrollee, 5, "10", b"", source="import")
    fpa(TEMPLATE_MAX_BYTES=4, ALLOWED_FINGER_INDEXES=[5, 6])
    with pytest.raises(InvalidInput, match="larger"):
        services.store_template(enrollee, 5, "10", b"12345", source="import")


def test_versioning_and_noop(make_enrollee):
    enrollee = make_enrollee()
    t1, changed = services.store_template(enrollee, 1, "10.0", b"A", source="import")
    assert changed and t1.version == 1 and t1.algorithm_version == "10"
    t2, changed = services.store_template(enrollee, 1, "10", b"A", source="import")
    assert not changed and t2.version == 1
    t3, changed = services.store_template(enrollee, 1, "10", b"B", source="import")
    assert changed and t3.version == 2


def test_inactive_enrollee_rejected(make_enrollee):
    enrollee = make_enrollee()
    services.deactivate_enrollee(enrollee)
    with pytest.raises(NotAllowed):
        services.store_template(enrollee, 1, "10", b"x", source="import")


# --------------------------------------------------------------------------- walk-up


def test_walk_up_enrollment_fans_out(simulator, make_enrollee):
    enrollee = make_enrollee(pin="1001")
    source = simulator(firmware="push2")
    other = simulator(firmware="push2")
    source.enroll_at_device("1001", 6, b"\x07" * 400, name="Ada")
    template = FingerprintTemplate.objects.get(enrollee=enrollee)
    assert template.source == "device" and template.get_data() == b"\x07" * 400
    src_device = Device.objects.get(serial_number=source.serial)
    assert template.source_device == src_device
    # the source device is marked synced without re-sending
    sync = DeviceEnrolleeSync.objects.get(device=src_device, enrollee=enrollee)
    assert sync.status == SyncStatus.SYNCED and sync.user_synced
    assert source.poll() == []
    # the other device receives user + template
    other.run_commands()
    assert other.templates[("1001", 6)] == b"\x07" * 400
    assert DeviceEnrolleeSync.objects.get(device__serial_number=other.serial).status == \
        SyncStatus.SYNCED


def test_walk_up_same_template_reconciles_only(simulator, make_enrollee):
    enrollee = make_enrollee(pin="1001")
    sim = simulator()
    services.store_template(enrollee, 6, "10", b"\x01" * 100, source="import")
    sim.run_commands()
    before = DeviceCommand.objects.count()
    sim.enroll_at_device("1001", 6, b"\x01" * 100)
    assert DeviceCommand.objects.count() == before
    assert FingerprintTemplate.objects.get().version == 1


def test_walk_up_rejections(simulator, make_enrollee, fpa):
    sim = simulator()
    sim.enroll_at_device("9999", 1, b"x" * 20)  # unknown pin
    make_enrollee(pin="2000", consent=False)
    sim.enroll_at_device("2000", 1, b"y" * 20)  # no consent
    reasons = set(DeviceEventLog.objects.filter(event_type="template_rejected")
                  .values_list("data__code", flat=True))
    assert reasons == {"unknown_pin", "consent_required"}
    assert not FingerprintTemplate.objects.exists()
    deletes = DeviceCommand.objects.filter(command_type="delete_template")
    assert deletes.count() == 2
    sim.run_commands()
    assert sim.templates == {}
    fpa(ACCEPT_DEVICE_ENROLLMENTS=False)
    sim.enroll_at_device("2000", 1, b"y" * 20)
    assert DeviceEventLog.objects.filter(event_type="template_rejected").count() == 2


def test_biodata_walk_up_records_algorithm_and_capability(simulator, make_enrollee):
    enrollee = make_enrollee(pin="3000")
    sim = simulator(firmware="biodata", fp_version="12")
    Device.objects.filter(serial_number=sim.serial).update(fp_algorithm_version="12")
    sim.enroll_at_device("3000", 2, b"\x0c" * 300)
    template = FingerprintTemplate.objects.get(enrollee=enrollee)
    assert template.algorithm_version == "12"
    assert Device.objects.get(serial_number=sim.serial).capabilities["biodata"] is True


# --------------------------------------------------------------------------- remote


def test_remote_enrollment_session(simulator, make_enrollee):
    enrollee = make_enrollee(pin="4000")
    sim = simulator()
    device = Device.objects.get(serial_number=sim.serial)
    session = services.start_enrollment_session(enrollee, device=device, fingers=[6, 7])
    assert session.status == SessionStatus.PENDING
    sim.run_commands()  # add_user + ENROLL_FP x2, device uploads the captured templates
    session.refresh_from_db()
    assert session.status == SessionStatus.COMPLETED
    assert sorted(session.fingers_captured) == [6, 7]
    assert FingerprintTemplate.objects.filter(enrollee=enrollee).count() == 2
    assert any(cmd.startswith("ENROLL_FP PIN=4000\tFID=6") for cmd in sim.executed)


def test_remote_enrollment_validation(make_enrollee, make_device):
    enrollee = make_enrollee()
    device = make_device()
    with pytest.raises(InvalidInput):
        services.start_enrollment_session(enrollee, device=device, fingers=[])
    with pytest.raises(InvalidInput):
        services.start_enrollment_session(enrollee)
    with pytest.raises(InvalidInput, match="duplicate"):
        services.start_enrollment_session(enrollee, device=device, fingers=[1, 1])
    inactive = make_device(status="disabled")
    with pytest.raises(NotAllowed):
        services.start_enrollment_session(enrollee, device=inactive, fingers=[1])
    no_consent = make_enrollee(consent=False)
    with pytest.raises(ConsentRequired):
        services.start_enrollment_session(no_consent, device=device, fingers=[1])


def test_remote_enroll_failure_fails_session(simulator, make_enrollee, fpa):
    fpa(COMMAND_MAX_RETRIES=0)
    enrollee = make_enrollee(pin="4100")
    sim = simulator()
    sim.fail_commands = {"ENROLL_FP": -3}
    device = Device.objects.get(serial_number=sim.serial)
    session = services.start_enrollment_session(enrollee, device=device, fingers=[6])
    sim.run_commands()
    session.refresh_from_db()
    assert session.status == SessionStatus.FAILED and "enroll command failed" in session.error


def test_new_session_supersedes_open_one(make_enrollee, make_device):
    enrollee = make_enrollee()
    device = make_device()
    first = services.start_enrollment_session(enrollee, device=device, fingers=[1])
    services.start_enrollment_session(enrollee, device=device, fingers=[2])
    first.refresh_from_db()
    assert first.status == SessionStatus.CANCELLED
    assert DeviceCommand.objects.get(session=first).status == "cancelled"


def test_session_expiry(make_enrollee, make_device):
    enrollee = make_enrollee()
    session = services.start_enrollment_session(enrollee, device=make_device(), fingers=[1])
    EnrollmentSession.objects.filter(pk=session.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1))
    assert services.expire_sessions() == 1
    session.refresh_from_db()
    assert session.status == SessionStatus.EXPIRED


# --------------------------------------------------------------------------- agent


@pytest.fixture
def agent():
    return services.create_agent("HR desk", algorithm_version="10")


def test_agent_session_flow(agent, make_enrollee, make_device):
    agent_obj, key = agent
    assert services.authenticate_agent(key) == agent_obj
    assert services.authenticate_agent("nope") is None
    enrollee = make_enrollee()
    make_device(algorithm="10")
    session = services.start_enrollment_session(enrollee, agent=agent_obj, fingers=[5, 6])
    services.claim_session(session, agent_obj)
    result = services.upload_agent_template(session, agent_obj, finger_index=5,
                                            algorithm_version="10",
                                            template=base64.b64encode(b"a" * 200).decode(),
                                            quality=80)
    assert result["remaining"] == [6]
    assert not FingerprintTemplate.objects.exists()  # staged, not stored yet
    with pytest.raises(InvalidInput, match="not captured"):
        services.complete_session(session, agent=agent_obj)
    services.upload_agent_template(session, agent_obj, finger_index=6, algorithm_version="10",
                                   template=b"b" * 200)
    services.complete_session(session, agent=agent_obj)
    session.refresh_from_db()
    assert session.status == SessionStatus.COMPLETED and "staged" not in session.metadata
    templates = FingerprintTemplate.objects.filter(enrollee=enrollee)
    assert templates.count() == 2
    assert templates.get(finger_index=5).quality == 80
    assert templates.get(finger_index=5).source == "desktop_agent"
    assert DeviceCommand.objects.filter(command_type="add_template").count() == 2
    with pytest.raises(SessionClosed):
        services.upload_agent_template(session, agent_obj, finger_index=5,
                                       algorithm_version="10", template=b"c")


def test_agent_cancel_discards_staged(agent, make_enrollee):
    agent_obj, _ = agent
    enrollee = make_enrollee()
    services.store_template(enrollee, 5, "10", b"GOOD", source="import")
    session = services.start_enrollment_session(enrollee, agent=agent_obj, fingers=[5])
    services.upload_agent_template(session, agent_obj, finger_index=5, algorithm_version="10",
                                   template=b"BAD!")
    services.cancel_session(session)
    assert FingerprintTemplate.objects.get().get_data() == b"GOOD"


def test_agent_rules(agent, make_enrollee, make_device, fpa):
    agent_obj, _ = agent
    other_agent, _ = services.create_agent("other", algorithm_version="10")
    enrollee = make_enrollee()
    session = services.start_enrollment_session(enrollee, agent=agent_obj, fingers=[5])
    with pytest.raises(NotAllowed):
        services.upload_agent_template(session, other_agent, finger_index=5,
                                       algorithm_version="10", template=b"x")
    with pytest.raises(InvalidInput, match="not requested"):
        services.upload_agent_template(session, agent_obj, finger_index=4,
                                       algorithm_version="10", template=b"x")
    with pytest.raises(InvalidInput, match="base64"):
        services.upload_agent_template(session, agent_obj, finger_index=5,
                                       algorithm_version="10", template="%%%")
    with pytest.raises(AlgorithmIncompatible, match="registered"):
        services.upload_agent_template(session, agent_obj, finger_index=5,
                                       algorithm_version="12", template=b"x")
    # every target device runs v12: a v10 template is useless unless the map allows it
    make_device(algorithm="12")
    with pytest.raises(AlgorithmIncompatible, match="any target device"):
        services.upload_agent_template(session, agent_obj, finger_index=5,
                                       algorithm_version="10", template=b"x")
    fpa(ALGORITHM_COMPATIBILITY_MAP={"12": ["10"]})
    services.upload_agent_template(session, agent_obj, finger_index=5, algorithm_version="10",
                                   template=b"x")
    fpa(FINGERS_REQUIRED_MIN=2, ALGORITHM_COMPATIBILITY_MAP={"12": ["10"]})
    with pytest.raises(InvalidInput, match="at least 2"):
        services.complete_session(session, agent=agent_obj)
    with pytest.raises(NotAllowed):
        services.agent_start_session(agent_obj, enrollee)
    fpa(AGENT_CAN_START_SESSIONS=True)
    started = services.agent_start_session(agent_obj, enrollee, fingers=[3])
    assert started.status == SessionStatus.IN_PROGRESS


def test_agent_key_rotation(agent):
    agent_obj, key = agent
    new_key = services.rotate_agent_key(agent_obj)
    assert services.authenticate_agent(key) is None
    assert services.authenticate_agent(new_key) == agent_obj
