"""Consistent error format for package endpoints::

    {"error": {"code": "consent_required", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from ..exceptions import FPAError


def error_response(code: str, message: str, http_status: int,
                   details: Any = None) -> Response:
    return Response({"error": {"code": code, "message": message, "details": details or {}}},
                    status=http_status)


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, FPAError):
        return error_response(exc.code, exc.message, exc.status_code, exc.details)
    if isinstance(exc, DjangoValidationError):
        exc = exceptions.ValidationError(detail=getattr(exc, "message_dict", None)
                                         or exc.messages)
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    if isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, exceptions.ValidationError):
        response.data = {"error": {"code": "invalid", "message": "Invalid input.",
                                   "details": response.data}}
        return response
    if isinstance(exc, exceptions.APIException):
        detail = exc.detail
        response.data = {"error": {"code": getattr(exc, "default_code", "error"),
                                   "message": str(detail) if not isinstance(detail, (dict, list))
                                   else "Error.",
                                   "details": detail if isinstance(detail, (dict, list)) else {}}}
    return response


__all__ = ["error_response", "exception_handler", "status"]
