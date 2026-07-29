"""Minimal Web Push helpers (VAPID env)."""
from __future__ import annotations

import os


def vapid_config() -> dict | None:
    """Return VAPID keys from env, or None if incomplete."""
    public = (os.environ.get("PARA_SCOPE_VAPID_PUBLIC_KEY") or "").strip()
    private = (os.environ.get("PARA_SCOPE_VAPID_PRIVATE_KEY") or "").strip()
    subject = (os.environ.get("PARA_SCOPE_VAPID_SUBJECT") or "mailto:admin@localhost").strip()
    if not public or not private:
        return None
    return {"public_key": public, "private_key": private, "subject": subject}
