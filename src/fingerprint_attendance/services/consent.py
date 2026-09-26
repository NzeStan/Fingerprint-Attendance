"""Biometric consent. Withdrawal deletes templates on every device and on the server."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction

from .. import payloads
from ..constants import ConsentState
from ..events import emit
from ..exceptions import NotAllowed
from ..utils.timeutils import now
from .audit import log_action


def give_consent(enrollee: Any, *, version: str, text_reference: str = "", method: str = "",
                 by: Any = None, given_at: datetime | None = None,
                 metadata: dict[str, Any] | None = None) -> Any:
    from ..models import ConsentRecord

    with transaction.atomic():
        record = ConsentRecord.objects.create(
            enrollee=enrollee, version=version, text_reference=text_reference, method=method,
            given_at=given_at or now(), captured_by=by if getattr(by, "pk", None) else None,
            metadata=metadata or {})
        enrollee.consent_state = ConsentState.GIVEN
        enrollee.save(update_fields=["consent_state", "updated_at"])
        log_action("consent.give", actor=by, obj=enrollee,
                   metadata={"version": version, "method": method})
    emit("consent_given", payloads.consent_payload(record), consent=record, enrollee=enrollee)
    return record


def withdraw_consent(enrollee: Any, *, by: Any = None, reason: str = "") -> list[Any]:
    """Withdraw every active consent record, cancel sessions, queue device deletes everywhere
    and delete stored templates."""
    from ..models import ConsentRecord
    from ..sync.engine import remove_enrollee_from_devices
    from .enrollees import cancel_open_sessions
    from .templates import delete_all_templates

    active = list(ConsentRecord.objects.filter(enrollee=enrollee, withdrawn_at__isnull=True))
    if not active and enrollee.consent_state != ConsentState.GIVEN:
        raise NotAllowed("the enrollee has no active consent to withdraw")
    with transaction.atomic():
        timestamp = now()
        for record in active:
            record.withdrawn_at = timestamp
            record.withdrawn_by = by if getattr(by, "pk", None) else None
            record.withdrawal_reason = reason
            record.save(update_fields=["withdrawn_at", "withdrawn_by", "withdrawal_reason",
                                       "updated_at"])
        enrollee.consent_state = ConsentState.WITHDRAWN
        enrollee.save(update_fields=["consent_state", "updated_at"])
        cancel_open_sessions(enrollee, reason="consent withdrawn")
        remove_enrollee_from_devices(enrollee, reason="consent withdrawn", everywhere=True,
                                     created_by=by)
        deleted = delete_all_templates(enrollee, by=by, propagate=False,
                                       reason="consent withdrawn")
        log_action("consent.withdraw", actor=by, obj=enrollee,
                   metadata={"reason": reason, "templates_deleted": deleted})
    for record in active:
        emit("consent_withdrawn", payloads.consent_payload(record), consent=record,
             enrollee=enrollee)
    return active
