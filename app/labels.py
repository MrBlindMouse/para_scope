"""Display labels for nameless pipeline steps (process → target)."""
from __future__ import annotations

from typing import Any


def action_label(action, fields_by_id: dict[int, Any] | None = None) -> str:
    """e.g. field_push → Uptime logbook, http_forward → https://…, web_push."""
    at = action.action_type or "?"
    cfg = action.config or {}
    if at == "field_push":
        fid = cfg.get("field_id")
        field = (fields_by_id or {}).get(int(fid)) if fid is not None else None
        if field is not None:
            return f"field_push → {field.name} {field.field_type}"
        if fid is not None:
            return f"field_push → #{fid}"
        return "field_push"
    if at == "http_forward":
        url = (cfg.get("url") or "").strip()
        if url:
            short = url if len(url) <= 48 else url[:45] + "…"
            return f"http_forward → {short}"
        return "http_forward"
    if at == "web_push":
        title = (cfg.get("title") or "").strip()
        return f"web_push → {title}" if title else "web_push"
    return at


def rule_label(rule, event_types_by_id: dict[int, Any] | None = None) -> str:
    """e.g. on on_success, fail · order 0 — or condition summary."""
    parts: list[str] = []
    et_ids = rule.event_type_ids or []
    if et_ids:
        names = []
        for eid in et_ids:
            et = (event_types_by_id or {}).get(eid)
            names.append(et.name if et else f"#{eid}")
        parts.append("on " + ", ".join(names))
    else:
        parts.append("on all events")
    cond = rule.conditions or {}
    if cond:
        keys = list(cond.keys())[:3]
        parts.append("if " + ", ".join(keys) + ("…" if len(cond) > 3 else ""))
    if not rule.enabled:
        parts.append("disabled")
    return " · ".join(parts)
