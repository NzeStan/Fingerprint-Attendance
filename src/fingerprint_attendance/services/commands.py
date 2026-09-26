"""Server -> device command queue.

* Commands are delivered in creation order (FIFO) and wait while a device is offline.
* ``dedupe_key`` makes queueing idempotent: an identical pending command is reused and a stale
  pending one for the same target is superseded (cancelled).
* Commands carrying template data are rendered only when handed to the device and are stored
  redacted.
* Negative return codes and unacknowledged commands are retried with exponential backoff up to
  ``COMMAND_MAX_RETRIES``; ``COMMAND_EXPIRY`` expires time-sensitive commands such as reboot.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from django.db import connection, transaction
from django.db.models import Q

from .. import payloads
from ..adms.adapter import CommandResult, get_adapter
from ..adms.commands import CommandBuildError, command_registry
from ..conf import settings
from ..constants import FINAL_COMMAND_STATUSES, CommandStatus, EventLogType
from ..events import after_commit, emit
from ..exceptions import InvalidInput, NotAllowed
from ..utils.logging import get_logger, redact
from ..utils.timeutils import now
from .audit import log_action

logger = get_logger(__name__)

RESPONSE_BODY_LIMIT = 10_000


def expiry_for(command_type: str) -> timedelta | None:
    mapping = settings.get_duration_map("COMMAND_EXPIRY")
    if command_type in mapping:
        return mapping[command_type]
    return mapping.get("default")


def _payload_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def queue_command(device: Any, command_type: str, payload: dict[str, Any] | None = None, *,
                  created_by: Any = None, correlation_id: str = "", dedupe_key: str = "",
                  enrollee: Any = None, session: Any = None, expires_in: timedelta | None = None,
                  max_attempts: int | None = None, audit: bool = True) -> Any:
    """Queue a command for ``device`` and return the DeviceCommand."""
    from ..models import DeviceCommand

    spec = command_registry.get(command_type)
    body = dict(payload or {})
    missing = [k for k in spec.required if body.get(k) in (None, "")]
    if missing:
        raise InvalidInput(f"{command_type}: missing payload keys {missing}",
                           details={"missing": missing})

    with transaction.atomic():
        if dedupe_key:
            pending = list(
                DeviceCommand.objects.select_for_update()
                .filter(device=device, dedupe_key=dedupe_key, status=CommandStatus.PENDING)
                .order_by("id")
            )
            for existing in pending:
                if existing.command_type == command_type and _payload_equal(existing.payload,
                                                                            body):
                    return existing
            for existing in pending:
                _finish(existing, CommandStatus.CANCELLED, error="superseded")

        adapter = get_adapter(device)
        if spec.sensitive:
            rendered = f"<{command_type} rendered at send time>"
        else:
            try:
                rendered = adapter.build_command(device, command_type, body)
            except CommandBuildError as exc:
                raise InvalidInput(str(exc)) from exc
        ttl = expires_in if expires_in is not None else expiry_for(command_type)
        command = DeviceCommand.objects.create(
            device=device,
            command_type=command_type,
            command_string=redact(rendered),
            payload=body,
            max_attempts=max_attempts,
            expires_at=now() + ttl if ttl else None,
            correlation_id=correlation_id or "",
            dedupe_key=dedupe_key,
            enrollee=enrollee,
            session=session,
            created_by=created_by if getattr(created_by, "pk", None) else None,
        )
        if spec.dangerous and audit:
            log_action(f"command.{command_type}", actor=created_by, obj=device,
                       metadata={"command_id": command.pk, "payload": _safe_payload(body)})
    emit("command_queued", payloads.command_payload(command), command=command)
    return command


def _safe_payload(body: dict[str, Any]) -> dict[str, Any]:
    return {k: (redact(v) if isinstance(v, str) else v) for k, v in body.items()}


def _max_attempts(command: Any) -> int:
    return int(command.max_attempts or (settings.COMMAND_MAX_RETRIES + 1))


def _backoff(attempts: int) -> timedelta:
    base = settings.COMMAND_RETRY_BACKOFF
    delay = base * (2 ** max(attempts - 1, 0))
    return min(delay, settings.COMMAND_RETRY_BACKOFF_MAX)


def _finish(command: Any, status: str, *, error: str = "") -> None:
    command.status = status
    command.completed_at = now()
    if error:
        command.error = error
    command.save(update_fields=["status", "completed_at", "error", "updated_at"])


def fetch_commands(device: Any, limit: int | None = None) -> list[tuple[int, str]]:
    """Hand out pending commands for ``device`` (marks them sent). Safe against concurrent
    polls: rows are locked (``select_for_update``, skipping locked rows where supported)."""
    from ..models import DeviceCommand

    limit = limit or settings.ADMS_MAX_COMMANDS_PER_REQUEST
    adapter = get_adapter(device)
    handed: list[tuple[int, str]] = []
    sent: list[Any] = []
    expired: list[Any] = []
    lock_kwargs: dict[str, Any] = {}
    if connection.features.has_select_for_update_skip_locked:
        lock_kwargs["skip_locked"] = True
    with transaction.atomic():
        current = now()
        queryset = (
            DeviceCommand.objects.select_for_update(**lock_kwargs)
            .filter(device=device, status=CommandStatus.PENDING)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=current))
            .order_by("id")
        )
        for command in queryset[: limit * 2]:
            if len(handed) >= limit:
                break
            if command.expires_at and command.expires_at <= current:
                _finish(command, CommandStatus.EXPIRED, error="expired before delivery")
                expired.append(command)
                continue
            try:
                text = adapter.build_command(device, command.command_type, command.payload)
            except CommandBuildError as exc:
                _finish(command, CommandStatus.CANCELLED, error=str(exc))
                continue
            command.status = CommandStatus.SENT
            command.attempts += 1
            command.sent_at = current
            command.next_attempt_at = None
            command.save(update_fields=["status", "attempts", "sent_at", "next_attempt_at",
                                        "updated_at"])
            handed.append((command.pk, text))
            sent.append(command)
    for command in sent:
        emit("command_sent", payloads.command_payload(command), command=command)
    for command in expired:
        emit("command_expired", payloads.command_payload(command), command=command)
    return handed


def record_results(device: Any, results: list[CommandResult]) -> int:
    """Apply ``devicecmd`` results. Idempotent: results for finished commands are ignored."""
    from ..models import DeviceCommand, DeviceEventLog

    applied = 0
    for result in results:
        with transaction.atomic():
            command = (DeviceCommand.objects.select_for_update()
                       .filter(device=device, pk=result.command_id).first())
            if command is None:
                DeviceEventLog.objects.create(
                    device=device, event_type=EventLogType.COMMAND_RESULT,
                    message=f"result for unknown command {result.command_id}",
                    data={"return": result.return_code, "cmd": result.cmd})
                continue
            if command.status in FINAL_COMMAND_STATUSES:
                continue
            command.return_code = result.return_code
            command.response_body = redact(result.body)[:RESPONSE_BODY_LIMIT]
            if result.return_code >= 0:
                command.status = CommandStatus.SUCCEEDED
                command.completed_at = now()
                command.save()
                after_commit(lambda c=command, r=result: _after_success(c, r))
            else:
                _register_failure(command, f"device returned {result.return_code}")
            applied += 1
    return applied


def _register_failure(command: Any, error: str) -> None:
    command.error = error
    if command.attempts < _max_attempts(command):
        command.status = CommandStatus.PENDING
        command.next_attempt_at = now() + _backoff(command.attempts)
        command.save()
        return
    command.status = CommandStatus.FAILED
    command.completed_at = now()
    command.save()
    after_commit(lambda c=command: _after_failure(c))


def _after_success(command: Any, result: CommandResult | None = None) -> None:
    emit("command_succeeded", payloads.command_payload(command), command=command)
    spec_tags = command_registry.get(command.command_type).tags \
        if command.command_type in command_registry else frozenset()
    try:
        if "sync" in spec_tags:
            from ..sync.engine import on_command_succeeded

            on_command_succeeded(command)
        if command.command_type == "info" and result is not None and result.data:
            from .devices import apply_device_info

            apply_device_info(command.device, result.data)
        if command.command_type in ("clear_logs", "clear_data"):
            from .devices import on_device_cleared

            on_device_cleared(command.device, command.command_type)
    except Exception:
        logger.exception("post-success handling failed for command %s", command.pk)


def _after_failure(command: Any) -> None:
    emit("command_failed", payloads.command_payload(command), command=command)
    try:
        spec_tags = command_registry.get(command.command_type).tags \
            if command.command_type in command_registry else frozenset()
        if "sync" in spec_tags:
            from ..sync.engine import on_command_failed

            on_command_failed(command)
        if "enroll" in spec_tags and command.session_id:
            from .enrollment import fail_session

            fail_session(command.session, f"enroll command failed ({command.return_code})")
    except Exception:
        logger.exception("post-failure handling failed for command %s", command.pk)


# --------------------------------------------------------------------------- maintenance


def expire_commands() -> int:
    from ..models import DeviceCommand

    current = now()
    count = 0
    for command in DeviceCommand.objects.select_related("device").filter(
        status__in=[CommandStatus.PENDING, CommandStatus.SENT], expires_at__lte=current
    ):
        _finish(command, CommandStatus.EXPIRED, error="expired")
        emit("command_expired", payloads.command_payload(command), command=command)
        count += 1
    return count


def retry_unacknowledged() -> int:
    """Commands sent but never acknowledged within ``COMMAND_ACK_TIMEOUT`` are retried."""
    from ..models import DeviceCommand

    cutoff = now() - settings.COMMAND_ACK_TIMEOUT
    count = 0
    for command in DeviceCommand.objects.select_related("device").filter(
        status=CommandStatus.SENT, sent_at__lte=cutoff
    ):
        with transaction.atomic():
            _register_failure(command, "no acknowledgement from device")
        count += 1
    return count


def cancel_command(command: Any, *, by: Any = None) -> Any:
    if command.status not in (CommandStatus.PENDING, CommandStatus.SENT):
        raise NotAllowed(f"command is already {command.status}")
    _finish(command, CommandStatus.CANCELLED, error="cancelled by user" if by else "cancelled")
    log_action("command.cancel", actor=by, obj=command.device,
               metadata={"command_id": command.pk, "type": command.command_type})
    return command


def retry_command(command: Any, *, by: Any = None) -> Any:
    if command.status not in (CommandStatus.FAILED, CommandStatus.EXPIRED,
                              CommandStatus.CANCELLED):
        raise NotAllowed(f"only failed/expired/cancelled commands can be retried "
                         f"(status {command.status})")
    ttl = expiry_for(command.command_type)
    command.status = CommandStatus.PENDING
    command.attempts = 0
    command.next_attempt_at = None
    command.completed_at = None
    command.return_code = None
    command.error = ""
    command.expires_at = now() + ttl if ttl else None
    command.save()
    log_action("command.retry", actor=by, obj=command.device,
               metadata={"command_id": command.pk, "type": command.command_type})
    emit("command_queued", payloads.command_payload(command), command=command)
    return command


def cancel_pending(device: Any, *, dedupe_prefix: str = "", command_types: tuple[str, ...] = (),
                   enrollee: Any = None, reason: str = "superseded") -> int:
    from ..models import DeviceCommand

    qs = DeviceCommand.objects.filter(device=device, status=CommandStatus.PENDING)
    if dedupe_prefix:
        qs = qs.filter(dedupe_key__startswith=dedupe_prefix)
    if command_types:
        qs = qs.filter(command_type__in=command_types)
    if enrollee is not None:
        qs = qs.filter(enrollee=enrollee)
    return qs.update(status=CommandStatus.CANCELLED, completed_at=now(), error=reason)
