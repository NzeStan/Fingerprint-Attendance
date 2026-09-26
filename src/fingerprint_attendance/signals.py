"""Django signals emitted by the package.

Every signal is sent with ``sender`` (the event name unless noted) and ``payload`` (a
JSON-serializable dict, identical to what hooks and webhooks receive), plus the model
instances listed below. See the signals reference in the docs.
"""

from __future__ import annotations

from django.dispatch import Signal

# devices ------------------------------------------------------------------ kwargs
device_registered = Signal()  # device
device_approved = Signal()  # device
device_disabled = Signal()  # device
device_online = Signal()  # device
device_offline = Signal()  # device
device_handshake = Signal()  # device
device_clock_drift = Signal()  # device, drift_seconds
device_log_capacity_warning = Signal()  # device, used, capacity

# commands
command_queued = Signal()  # command
command_sent = Signal()  # command
command_succeeded = Signal()  # command
command_failed = Signal()  # command
command_expired = Signal()  # command

# enrollment
enrollment_started = Signal()  # session
enrollment_completed = Signal()  # session
enrollment_expired = Signal()  # session
enrollment_cancelled = Signal()  # session
enrollee_activated = Signal()  # enrollee
enrollee_deactivated = Signal()  # enrollee

# templates / sync
template_stored = Signal()  # template, created (bool)
template_updated = Signal()  # template
template_deleted = Signal()  # enrollee, finger_index, algorithm_version
sync_queued = Signal()  # device, enrollee
sync_completed = Signal()  # device, enrollee
sync_failed = Signal()  # device, enrollee, error

# punches
punch_received = Signal()  # punch
punches_received = Signal()  # punches (list), device
punch_flagged = Signal()  # punch, flags
backlog_synced = Signal()  # device, count, start_date, end_date

# attendance
attendance_computed = Signal()  # attendance_day
calendar_changed = Signal()  # start_date, end_date, enrollee_ids

# consent
consent_given = Signal()  # consent, enrollee
consent_withdrawn = Signal()  # consent, enrollee
