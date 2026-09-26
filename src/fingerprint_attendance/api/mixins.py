"""Makes every package viewset configurable from settings."""

from __future__ import annotations

from typing import Any

from rest_framework.settings import api_settings

from ..conf import import_object, settings
from .filters import get_filter_backends
from .permissions import HasPerms


def resolve_exception_handler() -> Any:
    """``API_EXCEPTION_HANDLER``, or DRF's configured handler when ``None``. Service errors are
    converted to ``APIException`` first so the stock handler can render them."""
    path = settings.API_EXCEPTION_HANDLER
    if path is not None:
        return import_object(path, setting="API_EXCEPTION_HANDLER")
    drf_handler = api_settings.EXCEPTION_HANDLER

    def handler(exc: Exception, context: dict[str, Any]) -> Any:
        from rest_framework.exceptions import APIException

        from ..exceptions import FPAError

        if isinstance(exc, FPAError):
            converted = APIException(detail=exc.message, code=exc.code)
            converted.status_code = exc.status_code
            exc = converted
        return drf_handler(exc, context)  # type: ignore[operator]

    return handler


class ConfigurableViewSetMixin:
    """Resolves serializer, permissions, authentication, pagination, throttles and filters
    from ``FINGERPRINT_ATTENDANCE`` settings.

    * ``SERIALIZER_OVERRIDES[basename.action]`` or ``[basename]``
    * ``API_VIEWSET_PERMISSION_CLASSES[basename.action]`` or ``[basename]``, else
      ``API_PERMISSION_CLASSES``; ``action_permissions`` adds required model permissions for
      dangerous actions on top.
    """

    basename: str
    serializer_class: Any = None
    #: action -> serializer class (used when no override applies)
    action_serializers: dict[str, Any] = {}
    #: action -> required Django permissions (``app_label.codename``)
    action_permissions: dict[str, tuple[str, ...]] = {}
    filter_spec: dict[str, tuple[str, str]] = {}
    pagination_setting = "API_PAGINATION_CLASS"

    def get_serializer_class(self) -> Any:
        overrides = settings.SERIALIZER_OVERRIDES or {}
        action = getattr(self, "action", None) or ""
        path = overrides.get(f"{self.basename}.{action}")
        if path is None and action not in self.action_serializers:
            path = overrides.get(self.basename)
        if path:
            return import_object(path, setting="SERIALIZER_OVERRIDES")
        return self.action_serializers.get(action, self.serializer_class)

    def get_permissions(self) -> list[Any]:
        mapping = settings.API_VIEWSET_PERMISSION_CLASSES or {}
        action = getattr(self, "action", None)
        paths = (mapping.get(f"{self.basename}.{action}") or mapping.get(self.basename)
                 or settings.API_PERMISSION_CLASSES or [])
        permissions = [import_object(p, setting="API_PERMISSION_CLASSES")() for p in paths]
        required = self.action_permissions.get(action or "")
        if required:
            permissions.append(HasPerms(*required))
        return permissions

    def get_authenticators(self) -> list[Any]:
        classes = settings.API_AUTHENTICATION_CLASSES
        if classes is None:
            classes = getattr(self, "authentication_classes", None) or \
                api_settings.DEFAULT_AUTHENTICATION_CLASSES
        return [import_object(c, setting="API_AUTHENTICATION_CLASSES")() for c in classes]

    def get_throttles(self) -> list[Any]:
        classes = settings.API_THROTTLE_CLASSES
        if classes is None:
            classes = api_settings.DEFAULT_THROTTLE_CLASSES
        return [import_object(c, setting="API_THROTTLE_CLASSES")() for c in classes]

    @property
    def paginator(self) -> Any:
        if not hasattr(self, "_fpa_paginator"):
            path = getattr(settings, self.pagination_setting)
            self._fpa_paginator = import_object(path, setting=self.pagination_setting)() \
                if path else None
        return self._fpa_paginator

    def filter_queryset(self, queryset: Any) -> Any:
        for backend in get_filter_backends():
            queryset = backend().filter_queryset(self.request, queryset, self)  # type: ignore[attr-defined]
        return queryset

    def get_exception_handler(self) -> Any:
        return resolve_exception_handler()

    @property
    def actor(self) -> Any:
        return getattr(self.request, "user", None)  # type: ignore[attr-defined]
