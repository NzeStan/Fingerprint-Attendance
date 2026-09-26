# Pull mode

For devices on a LAN that cannot push, the server connects to them (default port 4370) using
`pyzk`.

```bash
pip install "django-fingerprint-attendance[pull]"
```

```python
FINGERPRINT_ATTENDANCE = {"PULL_ENABLED": True}
```

Create the device with `mode="pull"`, `pull_host`, and optionally `pull_port` and
`pull_comm_key`, then approve it.

| Command | Purpose |
|---|---|
| `fpa_pull_attendance [--device SN] [--full] [--loop]` | import punches newer than the device's `pull_last_record_at` marker; `--full` re-imports everything (dedupe prevents duplicates); `--loop` polls every `PULL_POLL_INTERVAL` |
| `fpa_live_capture [--device SN]` | long-running listener, one thread per device, reconnect with exponential backoff (up to `PULL_RECONNECT_BACKOFF_MAX`), graceful SIGINT/SIGTERM shutdown |
| `fpa_sync_device [--device SN] [--remove-extra]` | write missing users and templates directly (pull devices) or queue commands (push devices) |

**Gap-fill:** every time live capture (re)connects, it first imports from the marker and then
resumes live capture, so punches made while disconnected are never missed.

Pull-mode punches go through the same ingestion pipeline as ADMS (`source="pull"`), with the
same dedupe keys, so the same punch arriving by both routes is stored once.

Clock drift is measured on every connection (the device clock is read directly), and log
counts and capacity come from `read_sizes()`.

Run `fpa_live_capture` under a process manager (systemd, supervisord, a container) and restart
it on failure.

## Custom SDKs

Implement `fingerprint_attendance.pull.base.BasePullAdapter` (`connect`, `get_info`,
`get_users`, `get_templates`, `get_attendance`, `set_user`, `set_templates`, `delete_user`,
`set_time`, `live_capture`) and set `PULL_ADAPTER`. Tests can use
`fingerprint_attendance.testing.FakePullAdapter`.
