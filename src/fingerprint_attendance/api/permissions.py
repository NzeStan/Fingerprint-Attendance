from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission


class HasPerms(BasePermission):
    """Require Django model permissions (``app_label.codename``). Superusers always pass."""

    def __init__(self, *perms: str) -> None:
        self.perms = perms

    def has_permission(self, request: Any, view: Any) -> bool:
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.has_perms(self.perms))


class IsEnrollmentAgent(BasePermission):
    def has_permission(self, request: Any, view: Any) -> bool:
        from ..models import EnrollmentAgent

        return isinstance(getattr(request, "user", None), EnrollmentAgent)
