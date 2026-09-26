# Offline operation and catch-up

> The device is the source of truth for **when** a punch happened; the server is the source of
> truth for **who** is enrolled. A punch is never lost, never duplicated, and never re-timed
> because it arrived late.

## ADMS catch-up

- Per-device cursors (`attlog_stamp`, `operlog_stamp`, `attphoto_stamp`, `biodata_stamp`) are
  returned in the handshake, so the device uploads only what it has not delivered.
- A cursor advances **in the same database transaction** as the batch it covers. If the save
  fails, the reply is HTTP 500 and the device re-sends.
- Backlogs of thousands of lines are parsed in one pass and inserted with
  `bulk_create(ignore_conflicts=True)` on a unique dedupe hash, in chunks of
  `PUNCH_BULK_CHUNK_SIZE`. 2,000 punches take fewer than 60 queries.
- Queued server-to-device commands wait while a device is offline and are delivered in order
  on reconnect. `COMMAND_EXPIRY` expires time-sensitive types (reboot, set_time,
  enroll_fingerprint). User and template adds and deletes never expire unless you configure
  them to.

**Force re-upload:** `POST devices/{id}/reupload-logs/ {"from": "..."}`, the admin action, or
`fpa_reupload_device_logs --device SN --from 2026-01-01`. This resets the cursor and, with a
start date, queues `DATA QUERY ATTLOG`. Dedupe makes it safe.

## Pull catch-up

Each device keeps a `pull_last_record_at` marker. Live capture gap-fills from the marker on
every reconnect, and `--full` re-imports everything safely.

## Time integrity

- `punched_at` is always the device's recorded time converted with the device timezone (DST
  ambiguity resolves to the first occurrence). `received_at` is stored separately.
- `late_sync` flags punches received more than `LATE_SYNC_THRESHOLD` after they happened.
- `future` flags punches beyond `received_at + FUTURE_PUNCH_TOLERANCE`.
- Clock drift is recorded whenever the device clock is known (pull mode, or firmware that sends
  it). Drift beyond `CLOCK_DRIFT_WARNING_SECONDS` flags punches (`clock_drift`), emits
  `device_clock_drift`, and, with `AUTO_CORRECT_DEVICE_TIME`, queues `set_time` if it is within
  `MAX_AUTO_TIME_CORRECTION`.
- Offline periods across midnight, month ends or several days are handled per punch. Work
  dates come from the day boundary, which also supports overnight shifts.

## Late punches and recomputation

After a batch lands, every affected `(enrollee, work date)` is queued for processing through
the task backend. `backlog_synced` fires with the device, count and date range when the batch
has at least `BACKLOG_SIGNAL_THRESHOLD` punches or contains late ones, so consumers can
refresh dashboards or regenerate reports.

## Data safety

- The package never clears device logs on its own. `clear_logs` / `clear_data` are explicit,
  audited, permission-gated actions. They are refused (`unsafe_operation`) unless the device
  has reported its log count and the server holds every record since the last clear.
- `GET devices/reconciliation/` shows device count vs server count per device.
- Log capacity warnings fire when stored logs reach `LOG_CAPACITY_WARNING_RATIO` of capacity
  (reported or `DEFAULT_LOG_CAPACITY`). They are edge-triggered and also shown in health.

## Health

`GET /api/fingerprint/v1/health/` shows per device: online or offline, `last_seen_at`, offline
duration, pending commands, last punch time, pending-upload estimate, clock drift and the
capacity warning. It also totals devices, commands, unsynced enrollees and today's punches.
