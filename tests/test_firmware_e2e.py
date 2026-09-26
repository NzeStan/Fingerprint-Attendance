"""End-to-end: enroll -> sync -> punch -> attendance for every simulated firmware profile."""

from __future__ import annotations

from datetime import date

import pytest

from fingerprint_attendance import services
from fingerprint_attendance.models import AttendanceDay, Device, DeviceEnrolleeSync, Punch
from fingerprint_attendance.testing import FIRMWARE
from tests.conftest import local

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("firmware", sorted(FIRMWARE))
def test_full_flow(firmware, simulator, make_enrollee):
    enrollee = make_enrollee(pin="1001")
    enroll_station = simulator(firmware=firmware)
    door = simulator(firmware=firmware)
    # walk-up enrollment at one device fans out to the other
    enroll_station.enroll_at_device("1001", 6, b"\x11" * 512)
    door.run_commands()
    assert door.templates[("1001", 6)] == b"\x11" * 512
    assert DeviceEnrolleeSync.objects.filter(enrollee=enrollee, status="synced").count() == 2
    # punches at the door
    door.punch("1001", local(2026, 1, 5, 8, 2))
    door.punch("1001", local(2026, 1, 5, 17, 1))
    assert Punch.objects.filter(enrollee=enrollee).count() == 2
    day = AttendanceDay.objects.get(enrollee=enrollee, work_date=date(2026, 1, 5))
    assert day.status == "present"
    # deactivation removes the person from both devices
    services.deactivate_enrollee(enrollee)
    enroll_station.run_commands()
    door.run_commands()
    assert "1001" not in door.users and "1001" not in enroll_station.users
    uses_biodata = FIRMWARE[firmware]["template_line"] == "BIODATA"
    device = Device.objects.get(serial_number=door.serial)
    from fingerprint_attendance.adms.adapter import get_adapter

    assert get_adapter(device).supports_biodata(device) is uses_biodata
