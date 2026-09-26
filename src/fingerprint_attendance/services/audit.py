"""Audit log (template access, deletes, consent changes, dangerous commands, ...)."""

from __future__ import annotations

from typing import Any

from ..conf import settings
from ..utils.logging import get_logger, redact

logger = get_logger(__name__)


def _actor_repr(actor: Any) -> str:
    if actor is None:
        return "system"
    if getattr(actor, "is_authenticated", False) and hasattr(actor, "get_username"):
        return str(actor.get_username())
    return str(actor)[:200]


def log_action(action: str, *, actor: Any = None, obj: Any = None,
               metadata: dict[str, Any] | None = None, ip: str | None = None) -> Any:
    """Write an AuditLog row (no-op when ``AUDIT_LOG_ENABLED`` is off). Never pass template
    bytes or secrets in ``metadata``."""
    if not settings.AUDIT_LOG_ENABLED:
        return None
    from ..models import AuditLog

    user = actor if getattr(actor, "_meta", None) is not None and getattr(
        actor, "is_authenticated", False) and actor.__class__._meta.label_lower == _user_label() \
        else None
    clean_meta = {k: (redact(v) if isinstance(v, str) else v) for k, v in (metadata or {}).items()}
    entry = AuditLog.objects.create(
        actor=user,
        actor_repr=_actor_repr(actor),
        action=action,
        object_type=obj._meta.label_lower if obj is not None and hasattr(obj, "_meta") else "",
        object_id=str(getattr(obj, "uuid", getattr(obj, "pk", "")) or "") if obj is not None
        else "",
        object_repr=str(obj)[:200] if obj is not None else "",
        metadata=clean_meta,
        ip_address=ip,
    )
    logger.info("audit %s by %s on %s", action, entry.actor_repr, entry.object_repr)
    return entry


def _user_label() -> str:
    from django.conf import settings as dj

    return str(dj.AUTH_USER_MODEL).lower()
