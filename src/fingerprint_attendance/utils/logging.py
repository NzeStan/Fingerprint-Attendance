"""Package logging: every logger lives under ``fingerprint_attendance`` and redacts secrets.

Logger filters are not inherited by child loggers, so modules obtain loggers through
:func:`get_logger`, which attaches the redaction filter to each one.
"""

from __future__ import annotations

import logging
import re

LOGGER_NAMESPACE = "fingerprint_attendance"

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # key=value pairs in ADMS bodies and query strings
    (re.compile(r"(?i)\b(tmp|template|template_data|token|pushcommkey|api_key|agent_key|secret|"
                r"passwd|password|commkey)=([^\s\t&,]+)"), rf"\1={REDACTED}"),
    # JSON style
    (re.compile(r'(?i)"(template|template_data|tmp|token|key|api_key|secret|password)"'
                r'\s*:\s*"[^"]*"'), rf'"\1": "{REDACTED}"'),
    # Authorization headers
    (re.compile(r"(?i)\b(Agent|Bearer|Token|Basic)\s+[A-Za-z0-9._~+/=\-]{8,}"),
     rf"\1 {REDACTED}"),
    # long base64 blobs (templates) anywhere else
    (re.compile(r"[A-Za-z0-9+/]{120,}={0,2}"), REDACTED),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites the formatted message with secrets removed."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from ..conf import settings

            enabled = settings.LOG_REDACTION
        except Exception:  # settings not configured yet: redact to be safe
            enabled = True
        if not enabled:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        record.msg = redact(message)
        record.args = None
        return True


_FILTER = RedactingFilter()


def get_logger(name: str) -> logging.Logger:
    if not name.startswith(LOGGER_NAMESPACE):
        name = f"{LOGGER_NAMESPACE}.{name}"
    logger = logging.getLogger(name)
    if _FILTER not in logger.filters:
        logger.addFilter(_FILTER)
    return logger


def install_redaction() -> None:
    """Attach the filter to the root package logger (child loggers use :func:`get_logger`)."""
    get_logger(LOGGER_NAMESPACE)
