# django-fingerprint-attendance

A reusable **backend engine** for fingerprint attendance on Django and Django REST Framework.

| Area | What you get |
|---|---|
| Devices | ZKTeco ADMS/Push server (push 2.x–3.x), optional LAN pull mode (pyzk), auto-registration with approval, tokens, IP allowlists, rate limits |
| Enrollment | Walk-up at the device, remote-triggered enrollment, a desktop USB-reader agent API, consent enforcement, finger rules |
| Templates | Encrypted at rest (Fernet + rotation), versioned, checksummed, algorithm-aware (ZKFinger v10/v12) |
| Sync | Strategy-driven fan-out (all / device groups / custom), idempotent command queue, per-device sync state, resync and reconcile |
| Punches | One pipeline for every source: UTC normalisation, exact and window dedupe, anomaly flags, pluggable state resolver, bulk ingestion |
| Offline | Upload cursors that only advance after commit, safe re-upload, gap-fill, late-sync flags, clock drift detection and correction, backlog recompute |
| Attendance | Pluggable processor (default: first-in/last-out), day boundaries including overnight shifts, calendar/holiday/leave/schedule providers, status rules |
| Integration | Service layer, REST API, admin, signals, hooks, signed webhooks, Channels realtime, Celery / Django tasks, management commands |

Everything that could be a business decision is a setting or a dotted path. See the
[configuration reference](configuration.md).

**Out of scope** (these are yours to build on the hooks provided): reports, PDFs, dashboards,
payroll, mobile clock-in, face recognition, notifications.

Start with the [quickstart](quickstart.md).
