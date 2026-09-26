"""A custom TEMPLATE_STORAGE_BACKEND for tests (stands in for a vault/HSM)."""

from __future__ import annotations

from typing import Any

from fingerprint_attendance.crypto import BaseTemplateStorage


class MemoryStorage(BaseTemplateStorage):
    saved: dict[str, bytes] = {}

    def save(self, template: Any, plaintext: bytes) -> None:
        from fingerprint_attendance.utils.security import sha256_hex

        key = sha256_hex(plaintext)
        self.saved[key] = plaintext
        template.template_data = f"vault:{key}"

    def load(self, template: Any) -> bytes:
        return self.saved[template.template_data.split(":", 1)[1]]
