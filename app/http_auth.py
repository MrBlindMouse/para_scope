"""Shared outbound HTTP auth header injection (poll + Call URL).

Inbound webhook verification stays in webhook_verifiers.py.
"""

from __future__ import annotations

from typing import Any

from app.models import Secret
from app.security import decrypt_secret


def decrypt_secret_by_id(db, secret_id: int | None, *, label: str = "Secret") -> str:
    """Resolve and decrypt a Secret row by id. Raises ValueError if missing."""
    if not secret_id:
        raise ValueError(f"{label} is missing")
    secret = db.query(Secret).filter(Secret.id == secret_id).first()
    if not secret:
        raise ValueError(f"{label} not found")
    return decrypt_secret(secret.encrypted_value)


def inject_http_auth_headers(
    headers: dict,
    *,
    auth_mode: str,
    token: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict:
    """Apply bearer / key_secret into *headers*. Caller already resolved secrets.

    Mutates and returns the same dict. Modes other than bearer/key_secret are no-ops
    (callers handle basic/oauth locally).
    """
    cfg = config or {}
    mode = (auth_mode or "none").strip()
    if mode in ("", "none"):
        return headers

    if mode == "key_secret":
        if not api_key or not api_secret:
            raise ValueError("API key + secret auth needs both credentials")
        headers[cfg.get("api_key_header") or "X-Api-Key"] = api_key
        headers[cfg.get("api_secret_header") or "X-Api-Secret"] = api_secret
        return headers

    if mode == "bearer":
        if token is None:
            return headers
        header_name = cfg.get("auth_header") or "Authorization"
        prefix = cfg.get("auth_prefix")
        if prefix is None:
            prefix = "Bearer "
        headers[header_name] = f"{prefix}{token}"
    return headers
