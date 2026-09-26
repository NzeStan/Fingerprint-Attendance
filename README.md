# django-fingerprint-attendance

A reusable **Django + Django REST Framework engine for fingerprint attendance**. It covers
ZKTeco-compatible devices over the ADMS/Push protocol (plus optional LAN pull mode),
enrollment, encrypted template storage, multi-device template sync, and loss-free punch
ingestion. A pluggable attendance processor and work-calendar providers sit on top.

It is the **backend only**. You build the UI, reports, payroll and notifications, and the
package gives you a service layer, REST API, signals, hooks, webhooks and realtime events to
build them on. Every behaviour is configurable through settings or environment variables, and
every policy (sync scope, punch state, dedupe, day boundaries, statuses, calendars) can be
swapped with a dotted path.

- **Devices push to you** (`/iclock/cdata`, `getrequest`, `devicecmd`, plus push 3.x endpoints).
  Parsing is tolerant: a bad line never rejects a batch, and a failed save makes the device re-send.
- **Offline-safe.** Devices keep recording while the server is down. Upload cursors advance only
  after commit, dedupe makes re-sends harmless, and backlogs trigger recomputation for every
  affected day.
- **Three enrollment paths**: walk-up at the device, remote-triggered, and a desktop USB reader
  agent API. All three end in "template stored → fan-out to every in-scope device".
- **Privacy by default.** Templates are encrypted at rest (Fernet with key rotation) and never
  exposed by the API unless you opt in. Enrollment is consent-gated, withdrawal deletes
  templates everywhere, logs are redacted, and sensitive actions are audited.
- **Typed, tested, documented.** Ships `py.typed`, 300+ tests with a fake ADMS device
  simulator you can reuse in your own tests, and a full configuration reference.

## Install

```bash
pip install django-fingerprint-attendance            # core
pip install "django-fingerprint-attendance[all]"     # celery, channels, pull, filters, openapi, holidays
```

Extras: `celery`, `channels`, `pull` (pyzk), `filters` (django-filter), `openapi`
(drf-spectacular), `holidays`. `cryptography` is a core dependency.

Supported: Django 5.2 LTS (Python 3.10–3.14) and Django 6.0 / 6.1 (Python 3.12–3.14).

## 5-minute quickstart

```python
# settings.py
INSTALLED_APPS += ["rest_framework", "fingerprint_attendance"]
USE_TZ = True

FINGERPRINT_ATTENDANCE = {
    # "EMPLOYEE_MODEL": "hr.Employee",          # defaults to AUTH_USER_MODEL; set before migrate
    "TEMPLATE_ENCRYPTION_KEYS": [env("FPA_KEY")],  # python manage.py fpa_generate_key
}
```

```python
# urls.py
urlpatterns += [path("", include("fingerprint_attendance.urls"))]
# ADMS at /iclock/…   REST API at /api/fingerprint/v1/…
```

```bash
python manage.py migrate
python manage.py fpa_generate_key   # put the output in FPA_TEMPLATE_ENCRYPTION_KEYS
```

On the device (**Comm → Cloud Server / ADMS**), set the server address to your host and port,
and turn on "Domain name" if you use one. The device appears as *pending approval*. Approve it
in the admin or through `POST /api/fingerprint/v1/devices/{id}/approve/`.

```python
from fingerprint_attendance import services

enrollee = services.create_enrollee(employee)              # PIN generated
services.give_consent(enrollee, version="2026-01", method="paper")
services.start_enrollment_session(enrollee, device=device, fingers=[6])  # remote enroll
# ...or the person enrolls at the device menu; the template syncs everywhere automatically.
```

Punches flow in on their own. Read them from `Punch`, `GET /api/.../punches/`, or the
`punch_received` signal, hook, webhook or WebSocket event.

## How it fits together

```
 Enrollment                         Sync                              Punches
 ──────────                         ────                              ───────
 walk-up at device ─┐                                                 device ATTLOG ─┐
 remote ENROLL_FP ──┼─► store_template ─► sync strategy ─► queue per   pull import ───┼─► ingest pipeline
 desktop agent ─────┘   (validate,        (all / groups /   device     manual/import ─┘   parse → UTC → PIN →
                         consent,          custom)          commands                      dedupe → flags → state
                         encrypt,              │           (FIFO, dedupe,                 → bulk save → events
                         version)              ▼            retry, expiry)                → processor (per day)
                                        DeviceEnrolleeSync ◄─ devicecmd ack                    │
                                        ("who is on which device")                   AttendanceDay (+ calendar,
                                                                                      schedule, status rules)
```

## Documentation

The full docs live in [`docs/`](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/index.md) (MkDocs Material):

- [Configuration reference](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/configuration.md): every setting, env var, type and default
- [ADMS device setup](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/adms-setup.md) and [protocol notes](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/protocol.md)
- [Pull mode](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/pull-mode.md)
- [Desktop enrollment agent contract and example client](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/agent.md)
- [Offline operation and catch-up](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/offline.md)
- [Extending](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/extending.md): processors, sync strategies, resolvers, adapters, serializers
- [Signals, hooks, webhooks and realtime](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/events.md)
- [Calendar, holidays, leave and schedules](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/calendar.md)
- [REST API](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/api.md)
- [Security and privacy](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/security.md)
- [Deployment](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/deployment.md) and [upgrading](https://github.com/NzeStan/Fingerprint-Attendance/blob/main/docs/upgrading.md)

A complete [`example_project/`](https://github.com/NzeStan/Fingerprint-Attendance/tree/main/example_project) shows a custom employee model, Celery,
Channels, a custom attendance processor and a webhook receiver.

## Development

```bash
pip install -e ".[all]" pytest pytest-django pytest-cov
pytest                         # uses tests/settings.py (SQLite)
nox                            # lint, mypy, full Python × Django matrix
python scripts/gen_settings_docs.py   # regenerate the configuration reference
```

## License

MIT
