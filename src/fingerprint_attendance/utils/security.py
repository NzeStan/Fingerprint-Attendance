"""Secrets handling: token generation/hashing and client IP resolution."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import Iterable
from typing import Any


def generate_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_secret(secret: str) -> str:
    """SHA-256 of a high-entropy secret. Only hashes are stored; the plain value is shown once."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_secret(secret: str | None, hashed: str | None) -> bool:
    if not secret or not hashed:
        return False
    return hmac.compare_digest(hash_secret(secret), hashed)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ip_in(ip: str | None, networks: Iterable[str]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in networks:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Any, trusted_proxies: Iterable[str] = ()) -> str | None:
    """Remote address, honouring ``X-Forwarded-For`` only from trusted proxies."""
    remote = request.META.get("REMOTE_ADDR")
    proxies = list(trusted_proxies)
    if proxies and ip_in(remote, proxies):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        # walk right-to-left skipping our own proxies
        for candidate in reversed([p.strip() for p in forwarded.split(",") if p.strip()]):
            if not ip_in(candidate, proxies):
                return candidate
    return remote
