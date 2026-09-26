# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-09-26

### Added

- Configuration system: `FINGERPRINT_ATTENDANCE` dict, then `FPA_*` environment variables
  with typed casting, then defaults. Lazy and cached, reset on `setting_changed`. System checks
  `fpa.E000`–`fpa.E024` and `fpa.W001`–`fpa.W004`. Generated configuration reference.
- Models with UUID public ids and abstract bases: devices and groups, enrollees (configurable
  `EMPLOYEE_MODEL`), consent records, encrypted templates, per-device sync state, command
  queue, enrollment agents and sessions, immutable punches with adjustments, attendance days,
  holidays, work schedules, shift assignments, device events, audit log, webhooks.
- Template encryption at rest (Fernet/MultiFernet, rotation command, pluggable storage).
- ZKTeco ADMS/Push server: handshake, ATTLOG, OPERLOG, BIODATA, options, getrequest,
  devicecmd, ping, registry, push, querydata, rtdata. Tolerant parsing, a protocol adapter
  with per-device override, and an extensible command registry (FINGERTMP, BIODATA and legacy
  variants).
- Device authentication: auto-registration with approval, IP allowlists, trusted proxies,
  tokens, rate and size limits.
- Command queue: FIFO, dedupe/supersede, render-at-send for sensitive commands, retries with
  backoff, ack timeout, expiry.
- Enrollment: walk-up, remote-triggered, desktop agent API with staged uploads; consent and
  finger rules; algorithm compatibility map.
- Sync engine: strategies (all, device groups, custom), fan-out, incompatibility tracking,
  deletion propagation, backfill, resync, scope reconciliation.
- Punch ingestion pipeline: timezone normalisation, exact and window dedupe, anomaly flags,
  state resolvers, bulk ingestion, events, processing.
- Offline catch-up: cursors committed with data, forced re-upload, clock drift detection and
  auto-correction, backlog recompute, reconciliation report, safe log clearing, capacity
  warnings, health endpoint.
- Attendance processing: pluggable processor (first-in/last-out default), day boundaries
  (calendar, offset, overnight shift-aware), status rules, absence generation.
- Calendar providers: settings, database and `holidays`-library holidays chained with
  runtime overrides; leave and schedule providers; recompute on calendar change.
- REST API (versioned) with per-viewset/action overrides for serializers, permissions,
  filters, pagination, throttles and viewsets; agent API; OpenAPI annotations.
- Signals, hook registry, signed webhooks with retries, Channels realtime, task backends
  (sync, Celery, Django 6 tasks) and periodic jobs.
- Pull mode (pyzk) with import, live capture with gap-fill and reconnect, and device sync.
- Management commands, retention, audit log, log redaction, admin.
- Test utilities: ADMS device simulator and fake pull adapter.
