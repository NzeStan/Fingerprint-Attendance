"""REST API: resources, permissions, overrides, filters, errors and the agent API."""

from __future__ import annotations

import base64
from datetime import date

import pytest
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient

from fingerprint_attendance import services
from fingerprint_attendance.models import (
    AttendanceDay,
    Device,
    DeviceCommand,
    DeviceGroup,
    FingerprintTemplate,
    Holiday,
)
from tests.conftest import local

pytestmark = pytest.mark.django_db
V1 = "/api/fingerprint/v1"


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


# --------------------------------------------------------------------------- basics


def test_requires_admin_by_default(staff_user, django_user_model):
    client = APIClient()
    assert client.get(f"{V1}/devices/").status_code in (401, 403)
    user = django_user_model.objects.create_user("plain", password="pw")
    client.force_authenticate(user)
    response = client.get(f"{V1}/devices/")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_device_crud_and_actions(api, admin_user):
    group = DeviceGroup.objects.create(name="HQ")
    response = api.post(f"{V1}/devices/", {"serial_number": "API1", "name": "Front door",
                                           "groups": [str(group.uuid)],
                                           "timezone": "Africa/Lagos"}, format="json")
    assert response.status_code == 201, response.content
    device_id = response.json()["id"]
    device = Device.objects.get(serial_number="API1")
    assert device.status == "active" and list(device.groups.all()) == [group]
    assert api.post(f"{V1}/devices/", {"serial_number": "API2", "timezone": "Bad/Zone"},
                    format="json").status_code == 400
    pending = api.post(f"{V1}/devices/", {"serial_number": "API3", "approve": False},
                       format="json").json()
    assert pending["status"] == "pending_approval"
    assert api.post(f"{V1}/devices/{pending['id']}/approve/").json()["status"] == "active"
    assert api.patch(f"{V1}/devices/{device_id}/", {"location": "Lobby"},
                     format="json").json()["location"] == "Lobby"
    for action in ("set-time", "reboot", "info", "query-users"):
        assert api.post(f"{V1}/devices/{device_id}/{action}/").status_code == 202
    assert api.post(f"{V1}/devices/{device_id}/resync/", {"full": True},
                    format="json").status_code == 202
    assert api.get(f"{V1}/devices/{device_id}/sync-status/").json()["device_id"] == device_id
    assert api.get(f"{V1}/devices/{device_id}/health/").json()["serial_number"] == "API1"
    assert isinstance(api.get(f"{V1}/devices/{device_id}/events/").json(), list)
    token = api.post(f"{V1}/devices/{device_id}/issue-token/").json()["token"]
    assert len(token) > 20
    cmd = api.post(f"{V1}/devices/{device_id}/send-command/",
                   {"command_type": "set_option", "payload": {"key": "Delay", "value": 3}},
                   format="json")
    assert cmd.status_code == 202 and cmd.json()["command_string"] == "SET OPTION Delay=3"
    bad = api.post(f"{V1}/devices/{device_id}/send-command/", {"command_type": "delete_user"},
                   format="json")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid"
    assert api.post(f"{V1}/devices/{device_id}/disable/").json()["status"] == "disabled"
    assert api.get(f"{V1}/devices/reconciliation/").status_code == 200
    assert api.delete(f"{V1}/devices/{device_id}/").status_code == 204


def test_clear_logs_safety_via_api(api, make_device):
    device = make_device()
    response = api.post(f"{V1}/devices/{device.uuid}/clear-logs/")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsafe_operation"
    response = api.post(f"{V1}/devices/{device.uuid}/clear-data/", {"confirm": "wrong"},
                        format="json")
    assert response.status_code == 400
    Device.objects.filter(pk=device.pk).update(reported_punch_count=0)
    ok = api.post(f"{V1}/devices/{device.uuid}/clear-data/",
                  {"confirm": device.serial_number}, format="json")
    assert ok.status_code == 202


def test_reupload_logs_endpoint(api, make_device):
    device = make_device()
    response = api.post(f"{V1}/devices/{device.uuid}/reupload-logs/",
                        {"from": "2026-01-01T00:00:00Z"}, format="json")
    assert response.status_code == 202
    assert [c["command_type"] for c in response.json()] == ["query_attlog", "check"]


def test_dangerous_actions_need_model_permissions(staff_user, make_device, fpa):
    fpa(API_PERMISSION_CLASSES=["rest_framework.permissions.IsAuthenticated"])
    device = make_device()
    client = APIClient()
    client.force_authenticate(staff_user)
    assert client.get(f"{V1}/devices/").status_code == 200
    assert client.post(f"{V1}/devices/{device.uuid}/reboot/").status_code == 403
    grant(staff_user, "control_device")
    staff_user = type(staff_user).objects.get(pk=staff_user.pk)
    client.force_authenticate(staff_user)
    assert client.post(f"{V1}/devices/{device.uuid}/reboot/").status_code == 202
    assert client.post(f"{V1}/devices/{device.uuid}/send-command/",
                       {"command_type": "reboot"}, format="json").status_code == 403


def test_permission_override_map(api, staff_user, fpa):
    fpa(API_VIEWSET_PERMISSION_CLASSES={"devices": ["tests.testapp.api.AllowAll"],
                                        "punches.list": ["tests.testapp.api.DenyAll"]})
    client = APIClient()
    assert client.get(f"{V1}/devices/").status_code == 200  # anonymous allowed
    assert api.get(f"{V1}/punches/").status_code == 403


def test_serializer_and_viewset_overrides(api, make_device, fpa, settings):
    make_device("OVR1")
    fpa(SERIALIZER_OVERRIDES={"devices": "tests.testapp.api.BrandedDeviceSerializer"})
    assert api.get(f"{V1}/devices/").json()["results"][0]["brand"] == "ACME"
    fpa(SERIALIZER_OVERRIDES={"devices.retrieve": "tests.testapp.api.BrandedDeviceSerializer"})
    assert "brand" not in api.get(f"{V1}/devices/").json()["results"][0]
    from importlib import reload

    from django.urls import clear_url_caches

    import fingerprint_attendance.api.urls as api_urls
    import fingerprint_attendance.urls as root_urls
    import tests.urls as test_urls

    fpa(VIEWSET_OVERRIDES={"devices": "tests.testapp.api.ReadOnlyDeviceViewSet"})
    try:
        for module in (api_urls, root_urls, test_urls):
            reload(module)
        clear_url_caches()
        assert api.post(f"{V1}/devices/", {"serial_number": "X"},
                        format="json").status_code == 405
    finally:
        settings.FINGERPRINT_ATTENDANCE = {k: v for k, v in
                                           settings.FINGERPRINT_ATTENDANCE.items()
                                           if k != "VIEWSET_OVERRIDES"}
        for module in (api_urls, root_urls, test_urls):
            reload(module)
        clear_url_caches()


def test_authentication_throttle_pagination_settings(api, make_device, fpa, admin_user):
    for _ in range(3):
        make_device()
    fpa(API_PAGE_SIZE=2)
    data = api.get(f"{V1}/devices/").json()
    assert len(data["results"]) == 2 and data["count"] == 3
    fpa(API_PAGINATION_CLASS=None)
    assert isinstance(api.get(f"{V1}/devices/").json(), list)
    fpa(API_AUTHENTICATION_CLASSES=["rest_framework.authentication.BasicAuthentication"])
    client = APIClient()
    client.login(username="admin", password="pw")  # session auth no longer accepted
    assert client.get(f"{V1}/devices/").status_code in (401, 403)
    client.credentials(HTTP_AUTHORIZATION="Basic " + base64.b64encode(b"admin:pw").decode())
    assert client.get(f"{V1}/devices/").status_code == 200
    fpa(API_THROTTLE_CLASSES=["rest_framework.throttling.UserRateThrottle"])
    from rest_framework.settings import api_settings
    from rest_framework.throttling import UserRateThrottle

    rates = api_settings.DEFAULT_THROTTLE_RATES
    UserRateThrottle.THROTTLE_RATES = {**rates, "user": "1/min"}
    try:
        assert api.get(f"{V1}/devices/").status_code == 200
        assert api.get(f"{V1}/devices/").status_code == 429
    finally:
        UserRateThrottle.THROTTLE_RATES = rates


def test_exception_handler_setting(api, make_device, fpa):
    device = make_device()
    fpa(API_EXCEPTION_HANDLER=None)  # DRF's own format, service errors still mapped
    response = api.post(f"{V1}/devices/{device.uuid}/clear-logs/")
    assert response.status_code == 409 and "detail" in response.json()


# --------------------------------------------------------------------------- enrollees


def test_enrollee_lifecycle(api, make_employee, make_device):
    make_device()
    employee = make_employee(staff_number="EMP-9", name="Grace Hopper")
    response = api.post(f"{V1}/enrollees/", {"employee": "EMP-9"}, format="json")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["employee"] == "EMP-9" and body["employee_name"] == "Grace Hopper"
    assert body["device_pin"] == "1" and body["consent_state"] == "none"
    eid = body["id"]
    missing = api.post(f"{V1}/enrollees/", {"employee": "NOPE"}, format="json")
    assert missing.status_code == 400
    dup = api.post(f"{V1}/enrollees/", {"employee": "EMP-9"}, format="json")
    assert dup.json()["error"]["code"] == "already_enrolled"
    consent = api.post(f"{V1}/consents/", {"enrollee": eid, "version": "2026-01",
                                           "method": "paper"}, format="json")
    assert consent.status_code == 201
    assert api.get(f"{V1}/enrollees/{eid}/").json()["consent_state"] == "given"
    api.patch(f"{V1}/enrollees/{eid}/", {"display_name": "G. Hopper"}, format="json")
    assert api.get(f"{V1}/enrollees/{eid}/").json()["display_name"] == "G. Hopper"
    assert api.get(f"{V1}/enrollees/{eid}/fingers/").json()["enrolled"] == []
    assert api.get(f"{V1}/enrollees/{eid}/sync-status/").status_code == 200
    assert api.post(f"{V1}/enrollees/{eid}/resync/").status_code == 202
    assert len(api.get(f"{V1}/enrollees/{eid}/consents/").json()) == 1
    assert api.post(f"{V1}/enrollees/{eid}/deactivate/").json()["is_active"] is False
    assert api.post(f"{V1}/enrollees/{eid}/activate/").json()["is_active"] is True
    assert api.post(f"{V1}/enrollees/{eid}/withdraw-consent/", {"reason": "x"},
                    format="json").status_code == 200
    assert api.post(f"{V1}/consents/withdraw/", {"enrollee": eid},
                    format="json").status_code == 409
    assert api.delete(f"{V1}/enrollees/{eid}/").status_code == 204
    del employee


def test_enrollee_filters_both_backends(api, make_enrollee, fpa):
    group = DeviceGroup.objects.create(name="HQ")
    make_enrollee(groups=[group])
    make_enrollee(consent=False)
    for backend in ("builtin", "django_filter"):
        fpa(API_FILTER_BACKEND=backend)
        assert api.get(f"{V1}/enrollees/", {"group": str(group.uuid)}).json()["count"] == 1
        assert api.get(f"{V1}/enrollees/", {"consent_state": "none"}).json()["count"] == 1
        assert api.get(f"{V1}/enrollees/", {"is_active": "true"}).json()["count"] == 2
        assert api.get(f"{V1}/enrollees/", {"group": "not-a-uuid"}).status_code == 400


def test_filterset_override(api, make_enrollee, fpa):
    fpa(API_FILTER_BACKEND="django_filter",
        FILTERSET_OVERRIDES={"enrollees": "tests.testapp.filters.PinPrefixFilterSet"})
    make_enrollee(pin="100")
    make_enrollee(pin="200")
    assert api.get(f"{V1}/enrollees/", {"pin_prefix": "1"}).json()["count"] == 1


# --------------------------------------------------------------------------- templates


def test_templates_metadata_only_by_default(api, make_enrollee, fpa):
    enrollee = make_enrollee()
    payload = {"enrollee": str(enrollee.uuid), "finger_index": 6, "algorithm_version": "10",
               "template": base64.b64encode(b"\x01" * 100).decode(), "quality": 70}
    created = api.post(f"{V1}/templates/import/", payload, format="json")
    assert created.status_code == 201 and created.json()["changed"] is True
    again = api.post(f"{V1}/templates/import/", payload, format="json")
    assert again.status_code == 200 and again.json()["changed"] is False
    listing = api.get(f"{V1}/templates/").json()["results"]
    assert "template_data" not in listing[0]
    tid = listing[0]["id"]
    assert api.get(f"{V1}/templates/{tid}/data/").status_code == 404
    fpa(EXPOSE_TEMPLATE_DATA_IN_API=True)
    data = api.get(f"{V1}/templates/{tid}/data/").json()
    assert base64.b64decode(data["template_data"]) == b"\x01" * 100
    from fingerprint_attendance.models import AuditLog

    assert AuditLog.objects.filter(action="template.read").exists()
    bad = api.post(f"{V1}/templates/import/", {**payload, "template": "%%%"}, format="json")
    assert bad.status_code == 400
    assert api.delete(f"{V1}/templates/{tid}/").status_code == 204
    assert not FingerprintTemplate.objects.exists()


def test_template_data_needs_dedicated_permission(staff_user, make_enrollee, fpa):
    fpa(EXPOSE_TEMPLATE_DATA_IN_API=True,
        API_PERMISSION_CLASSES=["rest_framework.permissions.IsAuthenticated"])
    enrollee = make_enrollee()
    template, _ = services.store_template(enrollee, 1, "10", b"x" * 10, source="import")
    client = APIClient()
    client.force_authenticate(staff_user)
    assert client.get(f"{V1}/templates/{template.uuid}/data/").status_code == 403
    grant(staff_user, "view_template_data")
    client.force_authenticate(type(staff_user).objects.get(pk=staff_user.pk))
    assert client.get(f"{V1}/templates/{template.uuid}/data/").status_code == 200


# --------------------------------------------------------------------------- sessions & agent


def test_session_api_and_agent_contract(api, make_enrollee, make_device):
    make_device(algorithm="10")
    enrollee = make_enrollee()
    agent = api.post(f"{V1}/enrollment-agents/", {"name": "Desk 1", "algorithm_version": "10"},
                     format="json").json()
    key = agent["key"]
    session = api.post(f"{V1}/enrollment-sessions/", {"enrollee": str(enrollee.uuid),
                                                      "agent": agent["id"], "fingers": [5]},
                       format="json")
    assert session.status_code == 201, session.content
    sid = session.json()["id"]
    agent_client = APIClient()
    assert agent_client.get(f"{V1}/agent/me/").status_code in (401, 403)
    agent_client.credentials(HTTP_AUTHORIZATION=f"Agent {key}")
    me = agent_client.get(f"{V1}/agent/me/").json()
    assert me["name"] == "Desk 1" and me["allowed_finger_indexes"] == list(range(10))
    assert [s["id"] for s in agent_client.get(f"{V1}/agent/sessions/").json()] == [sid]
    assert agent_client.post(f"{V1}/agent/sessions/{sid}/claim/").json()["status"] == \
        "in_progress"
    upload = agent_client.post(f"{V1}/agent/sessions/{sid}/templates/", {
        "finger_index": 5, "algorithm_version": "10", "quality": 88,
        "template": base64.b64encode(b"\x09" * 300).decode()}, format="json")
    assert upload.status_code == 201 and upload.json()["remaining"] == []
    done = agent_client.post(f"{V1}/agent/sessions/{sid}/complete/")
    assert done.json()["status"] == "completed"
    assert FingerprintTemplate.objects.get().quality == 88
    closed = agent_client.post(f"{V1}/agent/sessions/{sid}/complete/")
    assert closed.status_code == 409 and closed.json()["error"]["code"] == "session_closed"
    assert agent_client.get(f"{V1}/agent/sessions/{sid}/").json()["status"] == "completed"
    # admin API views sessions and can cancel
    assert api.get(f"{V1}/enrollment-sessions/", {"agent": agent["id"]}).json()["count"] == 1
    rotated = api.post(f"{V1}/enrollment-agents/{agent['id']}/rotate-key/").json()["key"]
    assert agent_client.get(f"{V1}/agent/me/").status_code == 401
    agent_client.credentials(HTTP_AUTHORIZATION=f"Agent {rotated}")
    assert agent_client.get(f"{V1}/agent/me/").status_code == 200
    agent_client.credentials(HTTP_AUTHORIZATION="Agent")
    assert agent_client.get(f"{V1}/agent/me/").status_code == 401


def test_agent_start_and_cancel(api, make_enrollee, fpa):
    fpa(AGENT_CAN_START_SESSIONS=True)
    enrollee = make_enrollee(pin="321")
    _agent, key = services.create_agent("desk", algorithm_version="10")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Agent {key}")
    started = client.post(f"{V1}/agent/sessions/", {"enrollee": "321", "fingers": [1]},
                          format="json")
    assert started.status_code == 201
    sid = started.json()["id"]
    by_uuid = client.post(f"{V1}/agent/sessions/", {"enrollee": str(enrollee.uuid)},
                          format="json")
    assert by_uuid.status_code == 201
    assert client.post(f"{V1}/agent/sessions/", {"enrollee": "999"},
                       format="json").status_code == 400
    cancelled = client.post(f"{V1}/agent/sessions/{by_uuid.json()['id']}/cancel/")
    assert cancelled.json()["status"] == "cancelled"
    assert api.post(f"{V1}/enrollment-sessions/{sid}/cancel/").status_code == 200


def test_agent_rate_limit(make_enrollee, fpa):
    fpa(AGENT_RATE_LIMIT="2/m")
    _, key = services.create_agent("desk", algorithm_version="10")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Agent {key}")
    codes = [client.get(f"{V1}/agent/me/").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_remote_session_via_api(api, make_enrollee, make_device):
    device = make_device()
    enrollee = make_enrollee()
    response = api.post(f"{V1}/enrollment-sessions/", {"enrollee": str(enrollee.uuid),
                                                       "device": str(device.uuid),
                                                       "fingers": [6], "ttl_seconds": 120},
                        format="json")
    assert response.status_code == 201
    assert DeviceCommand.objects.filter(command_type="enroll_fingerprint").exists()
    no_consent = make_enrollee(consent=False)
    response = api.post(f"{V1}/enrollment-sessions/", {"enrollee": str(no_consent.uuid),
                                                       "device": str(device.uuid),
                                                       "fingers": [6]}, format="json")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "consent_required"


# --------------------------------------------------------------------------- commands & punches


def test_command_endpoints(api, make_device):
    device = make_device()
    cmd = services.queue_command(device, "check", {})
    listing = api.get(f"{V1}/commands/", {"device": str(device.uuid)}).json()
    assert listing["count"] == 1
    assert api.post(f"{V1}/commands/{cmd.uuid}/cancel/").json()["status"] == "cancelled"
    assert api.post(f"{V1}/commands/{cmd.uuid}/retry/").json()["status"] == "pending"
    assert api.post(f"{V1}/commands/{cmd.uuid}/retry/").status_code == 409


def test_punch_endpoints(api, simulator, make_enrollee, fpa):
    enrollee = make_enrollee(pin="1")
    sim = simulator()
    for hour in (8, 12, 17):
        sim.punch("1", local(2026, 1, 5, hour))
    sim.punch("2", local(2026, 1, 5, 9))
    page = api.get(f"{V1}/punches/").json()
    assert len(page["results"]) == 4 and "next" in page  # cursor pagination
    assert page["results"][0]["employee"] == enrollee.employee.staff_number or \
        page["results"][0]["employee"] is None
    for backend in ("builtin", "django_filter"):
        fpa(API_FILTER_BACKEND=backend)
        assert len(api.get(f"{V1}/punches/", {"enrollee": str(enrollee.uuid)}
                           ).json()["results"]) == 3
        assert len(api.get(f"{V1}/punches/", {"flag": "unknown_pin"}).json()["results"]) == 1
        assert len(api.get(f"{V1}/punches/", {"from": "2026-01-05T10:00:00+01:00",
                                              "to": "2026-01-05T18:00:00+01:00"}
                           ).json()["results"]) == 2
        assert len(api.get(f"{V1}/punches/", {"device_serial": sim.serial}
                           ).json()["results"]) == 4
    latest = api.get(f"{V1}/punches/latest/", {"after": 0, "limit": 2}).json()
    assert len(latest["results"]) == 2
    more = api.get(f"{V1}/punches/latest/", {"after": latest["next_after"]}).json()
    assert len(more["results"]) == 2
    assert api.get(f"{V1}/punches/latest/", {"after": "x"}).status_code == 400
    manual = api.post(f"{V1}/punches/manual/", {"enrollee": str(enrollee.uuid),
                                                "punched_at": "2026-01-06T08:00:00+01:00",
                                                "state": "check_in", "note": "forgot card"},
                      format="json")
    assert manual.status_code == 201 and manual.json()["flags"] == ["manual"]
    imported = api.post(f"{V1}/punches/import/", {"rows": [
        {"pin": "1", "punched_at": "2026-01-07 08:00:00"},
        {"pin": "1", "punched_at": "bad"}]}, format="json").json()
    assert imported["created"] == 1 and len(imported["rejected"]) == 1
    pid = manual.json()["id"]
    adj = api.post(f"{V1}/punches/{pid}/adjust/", {"action": "void", "reason": "test"},
                   format="json")
    assert adj.status_code == 201


def test_manual_punch_permission(staff_user, make_enrollee, fpa):
    fpa(API_PERMISSION_CLASSES=["rest_framework.permissions.IsAuthenticated"])
    enrollee = make_enrollee()
    client = APIClient()
    client.force_authenticate(staff_user)
    body = {"enrollee": str(enrollee.uuid), "punched_at": "2026-01-06T08:00:00+01:00"}
    assert client.post(f"{V1}/punches/manual/", body, format="json").status_code == 403


# --------------------------------------------------------------------------- attendance/calendar


def test_attendance_and_calendar(api, simulator, make_enrollee, fpa):
    enrollee = make_enrollee(pin="1")
    sim = simulator()
    sim.punch("1", local(2026, 1, 5, 8))
    sim.punch("1", local(2026, 1, 5, 17))
    days = api.get(f"{V1}/attendance-days/", {"enrollee": str(enrollee.uuid)}).json()
    assert days["count"] == 1 and days["results"][0]["worked_seconds"] == 9 * 3600
    assert api.get(f"{V1}/attendance-days/", {"status": "absent"}).json()["count"] == 0
    recompute = api.post(f"{V1}/attendance-days/recompute/", {"start": "2026-01-01",
                                                               "end": "2026-01-10"},
                         format="json")
    assert recompute.json()["days_computed"] == 1
    assert api.post(f"{V1}/attendance-days/recompute/", {"start": "2026-01-10",
                                                          "end": "2026-01-01"},
                    format="json").status_code == 400
    holiday = api.post(f"{V1}/holidays/", {"date": "2026-01-05", "name": "Surprise"},
                       format="json")
    assert holiday.status_code == 201
    assert AttendanceDay.objects.get(work_date=date(2026, 1, 5)).status == "worked_on_holiday"
    assert api.post(f"{V1}/holidays/", {"date": "2026-01-06", "name": "x",
                                        "scope": "device_group"},
                    format="json").status_code == 400
    calendar = api.get(f"{V1}/calendar/", {"enrollee": str(enrollee.uuid),
                                           "from": "2026-01-05", "to": "2026-01-11"}).json()
    types = [d["day_type"] for d in calendar["days"]]
    assert types == ["public_holiday", "workday", "workday", "workday", "workday", "weekend",
                     "weekend"]
    assert api.get(f"{V1}/calendar/", {"enrollee": str(enrollee.uuid)}).status_code == 400
    fpa(ATTENDANCE_PROCESSOR=None)
    assert api.get(f"{V1}/attendance-days/").status_code == 404
    assert Holiday.objects.count() == 1


def test_schedule_endpoints_gated(api, make_enrollee, fpa):
    assert api.get(f"{V1}/work-schedules/").status_code == 404
    fpa(SCHEDULE_MODELS_ENABLED=True)
    schedule = api.post(f"{V1}/work-schedules/", {"name": "Day", "start_time": "08:00",
                                                  "end_time": "16:00"}, format="json")
    assert schedule.status_code == 201
    enrollee = make_enrollee()
    assignment = api.post(f"{V1}/shift-assignments/", {"schedule": schedule.json()["id"],
                                                       "enrollee": str(enrollee.uuid),
                                                       "start_date": "2026-01-01"},
                          format="json")
    assert assignment.status_code == 201
    assert api.post(f"{V1}/shift-assignments/", {"schedule": schedule.json()["id"],
                                                 "start_date": "2026-01-01"},
                    format="json").status_code == 400


def test_health_groups_and_logs(api, make_device, make_enrollee):
    device = make_device()
    enrollee = make_enrollee()
    health = api.get(f"{V1}/health/").json()
    assert health["devices"]["total"] == 1
    group = api.post(f"{V1}/device-groups/", {"name": "Site A"}, format="json").json()
    gid = group["id"]
    api.post(f"{V1}/device-groups/{gid}/add-devices/", {"ids": [str(device.uuid)]},
             format="json")
    api.post(f"{V1}/device-groups/{gid}/add-enrollees/", {"ids": [str(enrollee.uuid)]},
             format="json")
    members = api.get(f"{V1}/device-groups/{gid}/members/").json()
    assert len(members["devices"]) == 1 and len(members["enrollees"]) == 1
    assert api.get(f"{V1}/device-groups/").json()["results"][0]["device_count"] == 1
    api.post(f"{V1}/device-groups/{gid}/remove-devices/", {"ids": [str(device.uuid)]},
             format="json")
    api.post(f"{V1}/device-groups/{gid}/remove-enrollees/", {"ids": [str(enrollee.uuid)]},
             format="json")
    assert api.post(f"{V1}/device-groups/{gid}/add-devices/", {"ids": "x"},
                    format="json").status_code == 400
    assert api.get(f"{V1}/audit-log/").status_code == 200
    assert api.get(f"{V1}/device-events/").status_code == 200


def test_webhook_endpoints(api, fpa):
    assert api.get(f"{V1}/webhooks/").status_code == 404
    fpa(WEBHOOKS_ENABLED=True, WEBHOOK_SIGNING_SECRET="s3cret",
        WEBHOOK_SENDER="tests.test_events.fake_sender")
    hook = api.post(f"{V1}/webhooks/", {"name": "HR", "url": "https://hr.example.com/hook",
                                        "events": ["punch_received"], "secret": "abc"},
                    format="json").json()
    assert hook["has_secret"] is True and "secret" not in hook
    test = api.post(f"{V1}/webhooks/{hook['id']}/test/").json()
    assert test["status"] == "succeeded"
    assert len(api.get(f"{V1}/webhooks/{hook['id']}/deliveries/").json()) == 1
    delivery_id = test["id"]
    assert api.post(f"{V1}/webhook-deliveries/{delivery_id}/redeliver/").status_code == 200
    assert api.get(f"{V1}/webhook-deliveries/", {"status": "succeeded"}).json()["count"] == 1


def test_openapi_schema_generates(api):
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(request=None, public=True)
    assert f"{V1}/punches/latest/" in schema["paths"]
    assert "AgentKey" in schema["components"]["securitySchemes"]
