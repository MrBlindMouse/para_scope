"""Minimal Web Push helpers (VAPID env + template substitution)."""
from __future__ import annotations

import os
import re

from app.fields import get_by_path


def vapid_config() -> dict | None:
    """Return VAPID keys from env, or None if incomplete."""
    public = (os.environ.get("PARA_SCOPE_VAPID_PUBLIC_KEY") or "").strip()
    private = (os.environ.get("PARA_SCOPE_VAPID_PRIVATE_KEY") or "").strip()
    subject = (os.environ.get("PARA_SCOPE_VAPID_SUBJECT") or "mailto:admin@localhost").strip()
    if not public or not private:
        return None
    return {"public_key": public, "private_key": private, "subject": subject}


_FIELD_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def render_template(text: str, data: dict | None) -> str:
    """Replace {{field}} / {{nested.path}} from event normalized_data."""
    if not text:
        return ""
    data = data or {}

    def _lookup(path: str):
        cur = get_by_path(data, path)
        return "" if cur is None else str(cur)

    return _FIELD_RE.sub(lambda m: _lookup(m.group(1)), text)
