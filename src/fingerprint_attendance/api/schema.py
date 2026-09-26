"""Optional drf-spectacular integration (``[openapi]`` extra). Without it these are no-ops."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import Any

HAS_SPECTACULAR = importlib.util.find_spec("drf_spectacular") is not None

extend_schema: Callable[..., Any]
OpenApiParameter: Any

if HAS_SPECTACULAR:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension
    from drf_spectacular.utils import OpenApiParameter as _Param
    from drf_spectacular.utils import extend_schema as _extend

    extend_schema = _extend
    OpenApiParameter = _Param

    class AgentKeyScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
        target_class = "fingerprint_attendance.api.authentication.AgentKeyAuthentication"
        name = "AgentKey"

        def get_security_definition(self, auto_schema: Any) -> dict[str, Any]:
            return {"type": "apiKey", "in": "header", "name": "Authorization",
                    "description": "`Agent <key>`"}

else:  # pragma: no cover - exercised only without the extra

    def _noop(*args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        def decorator(func: Any) -> Any:
            return func

        return decorator

    class _Parameter:
        QUERY = "query"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    extend_schema = _noop
    OpenApiParameter = _Parameter


__all__ = ["HAS_SPECTACULAR", "OpenApiParameter", "extend_schema"]
