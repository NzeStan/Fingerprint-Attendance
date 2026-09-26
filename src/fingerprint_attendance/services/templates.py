"""Fingerprint template storage. Every enrollment path ends in :func:`store_template`, which
validates, encrypts, versions and (optionally) fans the template out to devices."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from .. import payloads
from ..conf import settings
from ..constants import EventLogType, TemplateSource
from ..events import emit
from ..exceptions import ConsentRequired, InvalidInput, NotAllowed
from ..models import normalize_algorithm
from ..utils.logging import get_logger
from ..utils.security import sha256_hex
from .audit import log_action

logger = get_logger(__name__)


def validate_template(enrollee: Any, finger_index: int, data: bytes, *,
                      check_consent: bool = True) -> None:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise InvalidInput("template data is empty", code="empty_template")
    if len(data) > settings.TEMPLATE_MAX_BYTES:
        raise InvalidInput(f"template larger than {settings.TEMPLATE_MAX_BYTES} bytes",
                           code="template_too_large")
    if int(finger_index) not in (settings.ALLOWED_FINGER_INDEXES or []):
        raise InvalidInput(f"finger index {finger_index} is not allowed",
                           code="finger_not_allowed", details={"finger_index": finger_index})
    if check_consent and settings.ENROLLMENT_REQUIRE_CONSENT and not enrollee.has_consent:
        raise ConsentRequired()
    if not enrollee.is_active:
        raise NotAllowed("the enrollee is deactivated", code="enrollee_inactive")
    enrolled = set(enrollee.templates.values_list("finger_index", flat=True))
    if int(finger_index) not in enrolled and len(enrolled) >= settings.FINGERS_ALLOWED_MAX:
        raise InvalidInput(f"at most {settings.FINGERS_ALLOWED_MAX} fingers may be enrolled",
                           code="too_many_fingers")


def store_template(enrollee: Any, finger_index: int, algorithm_version: str, data: bytes, *,
                   source: str, source_device: Any = None, source_agent: Any = None,
                   quality: int | None = None, is_duress: bool = False, fan_out: bool = True,
                   exclude_devices: tuple[Any, ...] = (), by: Any = None) -> tuple[Any, bool]:
    """Create or update the template for (enrollee, finger, algorithm).

    Returns ``(template, changed)``; an identical upload (same checksum) is a no-op.
    """
    from ..models import FingerprintTemplate

    algorithm = normalize_algorithm(algorithm_version) or settings.DEFAULT_ALGORITHM_VERSION
    validate_template(enrollee, finger_index, bytes(data))
    checksum = sha256_hex(bytes(data))
    with transaction.atomic():
        template = (FingerprintTemplate.objects.select_for_update()
                    .filter(enrollee=enrollee, finger_index=finger_index,
                            algorithm_version=algorithm).first())
        created = template is None
        if template is not None and template.checksum == checksum:
            return template, False
        if template is None:
            template = FingerprintTemplate(enrollee=enrollee, finger_index=finger_index,
                                           algorithm_version=algorithm, version=1)
        else:
            template.version += 1
        template.set_data(bytes(data))
        template.source = source
        template.source_device = source_device
        template.source_agent = source_agent
        template.quality = quality
        template.is_duress = is_duress
        template.is_valid = True
        template.save()
        log_action("template.store", actor=by or source_agent or source_device, obj=enrollee,
                   metadata={"finger_index": finger_index, "algorithm": algorithm,
                             "version": template.version, "source": source})
    body = payloads.template_payload(template)
    emit("template_stored", {**body, "created": created}, template=template, created=created)
    if not created:
        emit("template_updated", body, template=template)
    if fan_out and settings.SYNC_ON_ENROLL:
        from ..sync.engine import sync_enrollee

        sync_enrollee(enrollee, exclude_devices=exclude_devices)
    return template, True


def delete_template(template: Any, *, by: Any = None, propagate: bool = True,
                    reason: str = "") -> None:
    from ..crypto import get_storage

    enrollee = template.enrollee
    finger, algorithm = template.finger_index, template.algorithm_version
    with transaction.atomic():
        get_storage().delete(template)
        template.delete()
        log_action("template.delete", actor=by, obj=enrollee,
                   metadata={"finger_index": finger, "algorithm": algorithm, "reason": reason})
    if propagate and settings.SYNC_ON_DELETE:
        from ..sync.engine import remove_template_from_devices

        remove_template_from_devices(enrollee, finger, algorithm, created_by=by)
    emit("template_deleted", {"enrollee_id": str(enrollee.uuid), "finger_index": finger,
                              "algorithm_version": algorithm, "reason": reason},
         enrollee=enrollee, finger_index=finger, algorithm_version=algorithm)


def delete_all_templates(enrollee: Any, *, by: Any = None, propagate: bool = True,
                         reason: str = "") -> int:
    count = 0
    for template in list(enrollee.templates.all()):
        delete_template(template, by=by, propagate=propagate, reason=reason)
        count += 1
    return count


def read_template_data(template: Any, *, by: Any = None, purpose: str = "") -> bytes:
    """Decrypt template bytes for an authorised caller; always audited."""
    data: bytes = template.get_data()
    log_action("template.read", actor=by, obj=template.enrollee,
               metadata={"template_id": str(template.uuid), "purpose": purpose})
    return data


# --------------------------------------------------------------------------- walk-up


def receive_device_template(device: Any, record: Any) -> Any:
    """A template uploaded by a device (OPERLOG FP/BIODATA, query results, pull import)."""
    from ..models import DeviceEventLog, Enrollee
    from ..sync.engine import mark_present, template_key

    algorithm = (normalize_algorithm(record.algorithm_version) or device.algorithm_major
                 or settings.DEFAULT_ALGORITHM_VERSION)

    def reject(reason: str, code: str, enrollee: Any = None) -> None:
        DeviceEventLog.objects.create(
            device=device, event_type=EventLogType.TEMPLATE_REJECTED, message=reason,
            data={"pin": record.pin, "finger_index": record.finger_index, "code": code})
        if settings.DELETE_REJECTED_DEVICE_TEMPLATES and code in (
            "consent_required", "finger_not_allowed", "unknown_pin", "too_many_fingers",
            "enrollee_inactive"
        ):
            from .commands import queue_command

            queue_command(device, "delete_template",
                          {"pin": record.pin, "finger_index": record.finger_index,
                           "reason": code},
                          enrollee=enrollee,
                          dedupe_key=f"reject:{record.pin}:{record.finger_index}")

    if not settings.ACCEPT_DEVICE_ENROLLMENTS:
        return None
    enrollee = Enrollee.objects.filter(device_pin=record.pin).first()
    if enrollee is None:
        reject("template for unknown PIN", "unknown_pin")
        return None
    existing = enrollee.templates.filter(finger_index=record.finger_index,
                                         algorithm_version=algorithm).first()
    if existing is not None and existing.checksum == sha256_hex(record.template):
        # the device holds what we hold: reconcile bookkeeping only
        mark_present(device, enrollee, template=existing)
        _progress_sessions(device, enrollee, record.finger_index)
        return existing
    try:
        template, _changed = store_template(
            enrollee, record.finger_index, algorithm, record.template,
            source=TemplateSource.DEVICE, source_device=device, is_duress=record.duress,
            fan_out=True, exclude_devices=(device,))
    except (InvalidInput, NotAllowed) as exc:
        reject(exc.message, exc.code, enrollee)
        return None
    DeviceEventLog.objects.create(
        device=device, event_type=EventLogType.TEMPLATE_RECEIVED,
        message=f"template {template_key(record.finger_index, algorithm)} for {record.pin}",
        data={"pin": record.pin, "finger_index": record.finger_index, "algorithm": algorithm,
              "version": template.version})
    mark_present(device, enrollee, template=template)
    _progress_sessions(device, enrollee, record.finger_index)
    return template


def _progress_sessions(device: Any, enrollee: Any, finger_index: int) -> None:
    from .enrollment import record_device_capture

    record_device_capture(device, enrollee, finger_index)
