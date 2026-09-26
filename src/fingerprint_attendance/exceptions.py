"""Service-layer exceptions. The API maps them to consistent error responses."""

from __future__ import annotations

from typing import Any


class FPAError(Exception):
    code = "error"
    status_code = 400

    def __init__(self, message: str = "", *, code: str | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message or self.__class__.__doc__ or self.code)
        self.message = message or (self.__class__.__doc__ or self.code).strip()
        if code:
            self.code = code
        self.details = details or {}


class InvalidInput(FPAError):
    """The request is invalid."""

    code = "invalid"


class NotAllowed(FPAError):
    """The operation is not allowed in the current state."""

    code = "not_allowed"
    status_code = 409


class ConsentRequired(NotAllowed):
    """Biometric consent is required before templates can be stored."""

    code = "consent_required"


class AlgorithmIncompatible(NotAllowed):
    """The template algorithm is not compatible with the target devices."""

    code = "algorithm_incompatible"


class SessionClosed(NotAllowed):
    """The enrollment session is no longer open."""

    code = "session_closed"


class UnsafeOperation(NotAllowed):
    """The operation could lose data and was refused."""

    code = "unsafe_operation"


class DeviceRejected(FPAError):
    """The device is not allowed to use the ADMS endpoints."""

    code = "device_rejected"
    status_code = 403
