"""Template encryption at rest (Fernet / MultiFernet with key rotation) and template storage.

Stored values are prefixed so encrypted and plaintext rows can coexist while a project turns
encryption on or rotates keys:

* ``fernet:<token>`` -- encrypted with one of ``TEMPLATE_ENCRYPTION_KEYS``
* ``plain:<base64>`` -- written while encryption was disabled
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.core.exceptions import ImproperlyConfigured

from .conf import settings
from .utils.logging import get_logger

if TYPE_CHECKING:
    from .models import FingerprintTemplate

logger = get_logger(__name__)

FERNET_PREFIX = "fernet:"
PLAIN_PREFIX = "plain:"


class TemplateDecryptionError(Exception):
    """Raised when stored template data cannot be decrypted with any configured key."""


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _multifernet() -> MultiFernet:
    keys = settings.TEMPLATE_ENCRYPTION_KEYS or []
    if not keys:
        raise ImproperlyConfigured(
            "TEMPLATE_ENCRYPTION_KEYS is empty but template encryption is enabled."
        )
    return MultiFernet([Fernet(k.encode("ascii") if isinstance(k, str) else k) for k in keys])


def encrypt(plaintext: bytes) -> str:
    """Encode template bytes for storage according to the current settings."""
    if settings.TEMPLATE_ENCRYPTION_ENABLED:
        token = _multifernet().encrypt(plaintext)
        return FERNET_PREFIX + token.decode("ascii")
    return PLAIN_PREFIX + base64.b64encode(plaintext).decode("ascii")


def decrypt(stored: str) -> bytes:
    if stored.startswith(FERNET_PREFIX):
        try:
            return _multifernet().decrypt(stored[len(FERNET_PREFIX):].encode("ascii"))
        except (InvalidToken, ImproperlyConfigured) as exc:
            raise TemplateDecryptionError("template data could not be decrypted") from exc
    if stored.startswith(PLAIN_PREFIX):
        return base64.b64decode(stored[len(PLAIN_PREFIX):])
    raise TemplateDecryptionError("unrecognised template storage format")


def rotate(stored: str) -> str:
    """Re-encrypt with the primary (first) key; converts plaintext rows when encryption is on."""
    if stored.startswith(FERNET_PREFIX):
        if not settings.TEMPLATE_ENCRYPTION_ENABLED:
            return encrypt(decrypt(stored))
        token = _multifernet().rotate(stored[len(FERNET_PREFIX):].encode("ascii"))
        return FERNET_PREFIX + token.decode("ascii")
    return encrypt(decrypt(stored))


def is_encrypted(stored: str) -> bool:
    return stored.startswith(FERNET_PREFIX)


# --------------------------------------------------------------------------- storage backends


class BaseTemplateStorage:
    """Where template bytes live. Subclass to use a vault/HSM; set TEMPLATE_STORAGE_BACKEND."""

    def save(self, template: FingerprintTemplate, plaintext: bytes) -> None:
        raise NotImplementedError

    def load(self, template: FingerprintTemplate) -> bytes:
        raise NotImplementedError

    def delete(self, template: FingerprintTemplate) -> None:
        """Called before the row is deleted."""

    def rotate(self, template: FingerprintTemplate) -> bool:
        """Re-encrypt ``template``; return True if it changed."""
        return False


class DatabaseTemplateStorage(BaseTemplateStorage):
    """Stores the (encrypted) template in ``FingerprintTemplate.template_data``."""

    def save(self, template: FingerprintTemplate, plaintext: bytes) -> None:
        template.template_data = encrypt(plaintext)

    def load(self, template: FingerprintTemplate) -> bytes:
        return decrypt(template.template_data)

    def rotate(self, template: FingerprintTemplate) -> bool:
        new = rotate(template.template_data)
        if new != template.template_data:
            template.template_data = new
            return True
        return False


def get_storage() -> BaseTemplateStorage:
    backend: Any = settings.import_("TEMPLATE_STORAGE_BACKEND")
    return backend() if isinstance(backend, type) else backend
