# Quickstart

## 1. Install and configure

```bash
pip install "django-fingerprint-attendance[celery,filters,openapi]"
```

```python
INSTALLED_APPS = [..., "rest_framework", "fingerprint_attendance"]
USE_TZ = True
TIME_ZONE = "Africa/Lagos"          # used as the default device timezone

FINGERPRINT_ATTENDANCE = {
    "EMPLOYEE_MODEL": "hr.Employee",            # optional; default AUTH_USER_MODEL
    "EMPLOYEE_LOOKUP_FIELD": "staff_number",    # how the API identifies employees
    "TEMPLATE_ENCRYPTION_KEYS": [os.environ["FPA_KEY"]],
}
```

!!! warning "Choose `EMPLOYEE_MODEL` before the first migrate"
    Like `AUTH_USER_MODEL`, the enrollee → employee foreign key is created by the first
    migration. The package's migrations never change when you pick a different model, but
    switching models later requires a manual data migration.

Any setting can also come from the environment: `FPA_TEMPLATE_ENCRYPTION_KEYS=key1,key2`,
`FPA_ADMS_URL_PREFIX=iclock/`, and so on.

## 2. URLs and database

```python
urlpatterns = [
    path("", include("fingerprint_attendance.urls")),  # /iclock/... and /api/fingerprint/v1/...
]
```

```bash
python manage.py fpa_generate_key     # store it in FPA_KEY
python manage.py migrate
python manage.py check                # fpa.E0xx errors explain misconfiguration
```

## 3. Connect a device

Follow [ADMS device setup](adms-setup.md). The device registers itself as
`pending_approval`. Approve it:

```python
from fingerprint_attendance import services
services.approve_device(Device.objects.get(serial_number="CKJF123456"), by=request.user)
```

Approval backfills every in-scope enrollee onto the device.

## 4. Enroll people

```python
enrollee = services.create_enrollee(employee)                   # device PIN auto-generated
services.give_consent(enrollee, version="2026-01", method="paper", by=request.user)

# a) at the device: the admin enrolls PIN <enrollee.device_pin> from the menu, or
# b) remotely:
services.start_enrollment_session(enrollee, device=device, fingers=[6, 1])
# c) at an HR desk with a USB reader: see the agent guide
```

The template is stored encrypted and queued to every other device in scope.

## 5. Consume punches

```python
from fingerprint_attendance.signals import punch_received

@receiver(punch_received)
def on_punch(sender, punch, payload, **kwargs):
    notify_dashboard(payload)
```

or `GET /api/fingerprint/v1/punches/latest/?after=<sequence>` for polling, webhooks, or the
WebSocket consumer. Daily results are in `AttendanceDay`
(`GET /api/fingerprint/v1/attendance-days/`).

## 6. Periodic jobs

With Celery:

```python
from fingerprint_attendance.tasks.celery import beat_schedule
app.conf.beat_schedule = {**app.conf.beat_schedule, **beat_schedule()}
FINGERPRINT_ATTENDANCE["TASK_BACKEND"] = "celery"
```

Without Celery, run the commands from cron: `fpa_check_devices` (every minute),
`fpa_expire_commands` and `fpa_retry_commands` (every few minutes),
`fpa_generate_absences` (daily), and `fpa_purge_retention` (daily).
