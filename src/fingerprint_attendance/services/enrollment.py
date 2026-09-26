"""Enrollment sessions: remote-triggered device enrollment and desktop enrollment agents.

Desktop agent uploads are *staged* on the session (encrypted) and only become templates when
the session completes, so a cancelled session never replaces an existing good template.
"""

from __future__ import annotations

import base64
import binascii
from datetime import timedelta
from typing import Any

from django.db import transaction

from .. import payloads
from ..conf import settings
from ..constants import OPEN_SESSION_STATUSES, DeviceStatus, SessionStatus, TemplateSource
from ..crypto import decrypt, encrypt
from ..events import emit
from ..exceptions import (
    AlgorithmIncompatible,
    ConsentRequired,
    InvalidInput,
    NotAllowed,
    SessionClosed,
)
from ..models import normalize_algorithm
from ..utils.security import generate_secret, hash_secret
from ..utils.timeutils import now
from .audit import log_action

STAGED = "staged"


# --------------------------------------------------------------------------- agents


def create_agent(name: str, *, algorithm_version: str, by: Any = None,
                 metadata: dict[str, Any] | None = None) -> tuple[Any, str]:
    """Create an enrollment agent. Returns ``(agent, plain_key)``; the key is shown once."""
    from ..models import EnrollmentAgent

    key = f"fpa_{generate_secret(30)}"
    agent = EnrollmentAgent.objects.create(
        name=name, algorithm_version=normalize_algorithm(algorithm_version) or algorithm_version,
        key_hash=hash_secret(key), key_prefix=key[:12], metadata=metadata or {},
        created_by=by if getattr(by, "pk", None) else None)
    log_action("agent.create", actor=by, obj=agent)
    return agent, key


def rotate_agent_key(agent: Any, *, by: Any = None) -> str:
    key = f"fpa_{generate_secret(30)}"
    agent.key_hash = hash_secret(key)
    agent.key_prefix = key[:12]
    agent.save(update_fields=["key_hash", "key_prefix", "updated_at"])
    log_action("agent.rotate_key", actor=by, obj=agent)
    return key


def authenticate_agent(key: str) -> Any:
    from ..models import EnrollmentAgent

    if not key:
        return None
    return EnrollmentAgent.objects.filter(key_hash=hash_secret(key), is_active=True).first()


# --------------------------------------------------------------------------- validation


def _validate_fingers(fingers: list[int] | None) -> list[int]:
    allowed = set(settings.ALLOWED_FINGER_INDEXES or [])
    fingers = [int(f) for f in (fingers or [])]
    bad = [f for f in fingers if f not in allowed]
    if bad:
        raise InvalidInput(f"finger indexes {bad} are not allowed", code="finger_not_allowed")
    if len(set(fingers)) != len(fingers):
        raise InvalidInput("duplicate finger indexes", code="duplicate_fingers")
    if len(fingers) > settings.FINGERS_ALLOWED_MAX:
        raise InvalidInput(f"at most {settings.FINGERS_ALLOWED_MAX} fingers",
                           code="too_many_fingers")
    return fingers


def check_algorithm_for_targets(enrollee: Any, algorithm_version: str) -> list[Any]:
    """Raise if no in-scope device can use templates of this algorithm. Returns the devices
    that cannot (they will be marked incompatible when syncing)."""
    from ..sync.engine import compatible_algorithms, is_compatible
    from ..sync.strategies import get_sync_strategy

    devices = list(get_sync_strategy().devices_for(enrollee))
    known = [d for d in devices if compatible_algorithms(d) is not None]
    incompatible = [d for d in devices if not is_compatible(d, algorithm_version)]
    if known and all(not is_compatible(d, algorithm_version) for d in known):
        raise AlgorithmIncompatible(
            f"algorithm {algorithm_version} is not accepted by any target device",
            details={"algorithm_version": algorithm_version,
                     "device_algorithms": sorted({d.algorithm_major for d in known})})
    return incompatible


# --------------------------------------------------------------------------- sessions


def start_enrollment_session(enrollee: Any, *, device: Any = None, agent: Any = None,
                             fingers: list[int] | None = None, by: Any = None,
                             ttl: timedelta | None = None) -> Any:
    """Open a session targeting a device (remote enroll) or a desktop agent."""
    from ..models import EnrollmentSession

    if (device is None) == (agent is None):
        raise InvalidInput("choose exactly one target: device or agent")
    if not enrollee.is_active:
        raise NotAllowed("the enrollee is deactivated", code="enrollee_inactive")
    if settings.ENROLLMENT_REQUIRE_CONSENT and not enrollee.has_consent:
        raise ConsentRequired()
    fingers = _validate_fingers(fingers)
    if device is not None:
        if device.status != DeviceStatus.ACTIVE:
            raise NotAllowed("the device is not active", code="device_inactive")
        if not fingers:
            raise InvalidInput("choose at least one finger to enroll on the device")
    if agent is not None and not agent.is_active:
        raise NotAllowed("the agent is disabled", code="agent_inactive")

    from .enrollees import cancel_open_sessions

    cancel_open_sessions(enrollee, reason="superseded by a new session")
    with transaction.atomic():
        session = EnrollmentSession.objects.create(
            enrollee=enrollee, device=device, agent=agent, fingers_requested=fingers,
            expires_at=now() + (ttl or settings.ENROLLMENT_SESSION_TTL),
            initiated_by=by if getattr(by, "pk", None) else None)
        if device is not None:
            _queue_remote_enroll(session, by=by)
        log_action("enrollment.start", actor=by, obj=enrollee,
                   metadata={"session": str(session.uuid),
                             "target": "device" if device is not None else "agent",
                             "fingers": fingers})
    emit("enrollment_started", payloads.session_payload(session), session=session)
    return session


def _queue_remote_enroll(session: Any, *, by: Any = None) -> None:
    from ..sync.engine import user_payload
    from .commands import queue_command

    enrollee, device = session.enrollee, session.device
    # the user must exist on the device before it can enroll a finger for them
    queue_command(device, "add_user", user_payload(enrollee), enrollee=enrollee,
                  dedupe_key=f"user:{enrollee.pk}", created_by=by, audit=False)
    for finger in session.fingers_requested:
        queue_command(device, "enroll_fingerprint",
                      {"pin": enrollee.device_pin, "finger_index": finger},
                      enrollee=enrollee, session=session, created_by=by,
                      expires_in=session.expires_at - now(), audit=False)


def _ensure_open(session: Any) -> None:
    if session.status not in OPEN_SESSION_STATUSES:
        raise SessionClosed(f"session is {session.status}")
    if session.is_expired:
        expire_session(session)
        raise SessionClosed("session has expired")


def claim_session(session: Any, agent: Any) -> Any:
    """Agent marks a pending session as in progress."""
    _ensure_open(session)
    if session.agent_id != agent.pk:
        raise NotAllowed("this session belongs to another agent", code="wrong_agent")
    if session.status == SessionStatus.PENDING:
        session.status = SessionStatus.IN_PROGRESS
        session.save(update_fields=["status", "updated_at"])
    return session


def agent_start_session(agent: Any, enrollee: Any, *, fingers: list[int] | None = None) -> Any:
    if not settings.AGENT_CAN_START_SESSIONS:
        raise NotAllowed("agents may not start sessions (AGENT_CAN_START_SESSIONS)",
                         code="agent_cannot_start")
    session = start_enrollment_session(enrollee, agent=agent, fingers=fingers, by=None)
    return claim_session(session, agent)


def upload_agent_template(session: Any, agent: Any, *, finger_index: int,
                          algorithm_version: str, template: bytes | str,
                          quality: int | None = None) -> dict[str, Any]:
    """Stage a template captured by a desktop agent."""
    from .templates import validate_template

    _ensure_open(session)
    if session.agent_id != agent.pk:
        raise NotAllowed("this session belongs to another agent", code="wrong_agent")
    if isinstance(template, str):
        try:
            data = base64.b64decode(template, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidInput("template must be base64", code="invalid_base64") from exc
    else:
        data = bytes(template)
    finger_index = int(finger_index)
    if session.fingers_requested and finger_index not in session.fingers_requested:
        raise InvalidInput(f"finger {finger_index} was not requested in this session",
                           code="finger_not_requested")
    algorithm = normalize_algorithm(algorithm_version) or algorithm_version
    if agent.algorithm_version and normalize_algorithm(agent.algorithm_version) != algorithm:
        raise AlgorithmIncompatible(
            f"agent is registered for algorithm {agent.algorithm_version}, got {algorithm}")
    check_algorithm_for_targets(session.enrollee, algorithm)
    validate_template(session.enrollee, finger_index, data)
    with transaction.atomic():
        session.refresh_from_db()
        metadata = dict(session.metadata or {})
        staged = dict(metadata.get(STAGED, {}))
        staged[str(finger_index)] = {"algorithm": algorithm, "quality": quality,
                                     "data": encrypt(data)}
        metadata[STAGED] = staged
        session.metadata = metadata
        session.status = SessionStatus.IN_PROGRESS
        captured = sorted({*session.fingers_captured, finger_index})
        session.fingers_captured = captured
        session.save(update_fields=["metadata", "status", "fingers_captured", "updated_at"])
    return {"finger_index": finger_index, "algorithm_version": algorithm,
            "captured": captured, "remaining": [f for f in session.fingers_requested
                                                if f not in captured]}


def complete_session(session: Any, *, agent: Any = None, by: Any = None) -> Any:
    """Store staged templates (agent sessions) and fan them out."""
    from ..sync.engine import sync_enrollee
    from .templates import store_template

    _ensure_open(session)
    if agent is not None and session.agent_id != agent.pk:
        raise NotAllowed("this session belongs to another agent", code="wrong_agent")
    enrollee = session.enrollee
    staged = dict((session.metadata or {}).get(STAGED, {}))
    if session.agent_id:
        missing = [f for f in session.fingers_requested if str(f) not in staged]
        if missing:
            raise InvalidInput(f"fingers {missing} were requested but not captured",
                               code="fingers_missing")
        existing = set(enrollee.templates.values_list("finger_index", flat=True))
        total = existing | {int(f) for f in staged}
        if len(total) < settings.FINGERS_REQUIRED_MIN:
            raise InvalidInput(
                f"at least {settings.FINGERS_REQUIRED_MIN} fingers are required "
                f"({len(total)} enrolled)", code="too_few_fingers")
        with transaction.atomic():
            for finger, item in sorted(staged.items(), key=lambda kv: int(kv[0])):
                store_template(enrollee, int(finger), item["algorithm"], decrypt(item["data"]),
                               source=TemplateSource.DESKTOP_AGENT, source_agent=session.agent,
                               quality=item.get("quality"), fan_out=False, by=by or agent)
            _close(session, SessionStatus.COMPLETED)
        if settings.SYNC_ON_ENROLL:
            sync_enrollee(enrollee)
    else:
        _close(session, SessionStatus.COMPLETED)
    log_action("enrollment.complete", actor=by or agent, obj=enrollee,
               metadata={"session": str(session.uuid), "fingers": session.fingers_captured})
    emit("enrollment_completed", payloads.session_payload(session), session=session)
    return session


def _close(session: Any, status: str, error: str = "") -> None:
    metadata = dict(session.metadata or {})
    metadata.pop(STAGED, None)  # never keep staged biometric data on a closed session
    session.metadata = metadata
    session.status = status
    session.completed_at = now()
    if error:
        session.error = error
    session.save(update_fields=["metadata", "status", "completed_at", "error", "updated_at"])


def _cancel_session_commands(session: Any, reason: str) -> None:
    from ..constants import CommandStatus
    from ..models import DeviceCommand

    DeviceCommand.objects.filter(session=session, status=CommandStatus.PENDING).update(
        status=CommandStatus.CANCELLED, completed_at=now(), error=reason)


def cancel_session(session: Any, *, reason: str = "cancelled", by: Any = None,
                   status: str = SessionStatus.CANCELLED) -> Any:
    if session.status not in OPEN_SESSION_STATUSES:
        return session
    _close(session, status, reason)
    _cancel_session_commands(session, reason)
    event = "enrollment_expired" if status == SessionStatus.EXPIRED else "enrollment_cancelled"
    if by is not None:
        log_action("enrollment.cancel", actor=by, obj=session.enrollee,
                   metadata={"session": str(session.uuid), "reason": reason})
    emit(event, payloads.session_payload(session), session=session)
    return session


def expire_session(session: Any) -> Any:
    return cancel_session(session, reason="expired", status=SessionStatus.EXPIRED)


def fail_session(session: Any, error: str) -> Any:
    return cancel_session(session, reason=error, status=SessionStatus.FAILED)


def expire_sessions() -> int:
    from ..models import EnrollmentSession

    count = 0
    for session in EnrollmentSession.objects.filter(status__in=OPEN_SESSION_STATUSES,
                                                    expires_at__lte=now()):
        expire_session(session)
        count += 1
    return count


def record_device_capture(device: Any, enrollee: Any, finger_index: int) -> None:
    """A device uploaded a template: progress any open remote session for it."""
    from ..models import EnrollmentSession

    for session in EnrollmentSession.objects.filter(device=device, enrollee=enrollee,
                                                    status__in=OPEN_SESSION_STATUSES):
        if session.is_expired:
            expire_session(session)
            continue
        captured = sorted({*session.fingers_captured, int(finger_index)})
        session.fingers_captured = captured
        session.status = SessionStatus.IN_PROGRESS
        session.save(update_fields=["fingers_captured", "status", "updated_at"])
        if all(f in captured for f in session.fingers_requested):
            complete_session(session)
