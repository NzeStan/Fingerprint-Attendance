# Management commands

| Command | Purpose | Suggested schedule |
|---|---|---|
| `fpa_check_devices` | mark silent devices offline (`device_offline`) | every minute |
| `fpa_expire_commands` | expire commands past `COMMAND_EXPIRY` and stale enrollment sessions | every 5 min |
| `fpa_retry_commands` | retry unacknowledged commands and due webhook deliveries | every minute |
| `fpa_purge_retention [--dry-run]` | apply `RETENTION_*` | daily |
| `fpa_generate_absences [--date] [--force]` | absent/leave/off rows for a date (default yesterday) | daily after cut-off |
| `fpa_recompute_attendance --from --to [--enrollee PIN/ID ...]` | recompute `AttendanceDay` | on demand |
| `fpa_resync_all [--full]` | queue missing users/templates on all devices | on demand |
| `fpa_sync_device [--device SN] [--full] [--remove-extra]` | sync specific devices (pull: direct write) | on demand |
| `fpa_reupload_device_logs --device SN [--from ISO] [--table ATTLOG]` | force re-upload of stored logs | on demand |
| `fpa_pull_attendance [--device] [--full] [--loop]` | pull-mode import | cron or `--loop` |
| `fpa_live_capture [--device] [--timeout]` | pull-mode live listener | always-on service |
| `fpa_rotate_template_keys [--batch-size]` | re-encrypt templates with the newest key | after adding a key |
| `fpa_generate_key` | print a new Fernet key | setup |
| `fpa_create_agent NAME [--algorithm 10]` | create a desktop enrollment agent (prints key once) | setup |

With Celery, `fingerprint_attendance.tasks.celery.beat_schedule()` schedules the periodic
ones for you.
