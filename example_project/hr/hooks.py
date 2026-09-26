from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def device_name(employee: Any) -> str:
    """EMPLOYEE_DISPLAY_FIELD callable: 'LASTNAME F.' fits small device screens."""
    return f"{employee.last_name.upper()} {employee.first_name[:1]}."


def log_backlog(event: str, payload: dict[str, Any]) -> None:
    """HOOKS entry: a device came back online with a backlog; refresh reports here."""
    logger.info("backlog of %s punches from %s covering %s..%s", payload["count"],
                (payload.get("device") or {}).get("serial_number"), payload["start_date"],
                payload["end_date"])
