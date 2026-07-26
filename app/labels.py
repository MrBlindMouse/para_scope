"""Display labels for nameless pipeline steps (process → target)."""
from __future__ import annotations

from typing import Any

ACTION_TYPE_LABELS = {
    "field_push": "Update field",
    "http_forward": "Call URL",
    "web_push": "Browser notification",
}

FIELD_TYPE_LABELS = {
    "logbook": "Logbook",
    "counter": "Counter",
    "value": "Value",
    "toggle": "Toggle",
}

HTTP_METHOD_LABELS = {
    "http_get": "GET",
    "http_post": "POST",
    "http_put": "PUT",
    "http_delete": "DELETE",
}

OPERATOR_LABELS = {
    "equals": "Equals (=)",
    "not": "Not equal (≠)",
    "gt": "Greater than (>)",
    "lt": "Less than (<)",
    "contains": "Contains",
    "regex": "Matches pattern (regex)",
}


def action_type_label(slug: str) -> str:
    return ACTION_TYPE_LABELS.get(slug, slug)


def field_type_label(slug: str) -> str:
    return FIELD_TYPE_LABELS.get(slug, slug)


def http_method_label(slug: str) -> str:
    return HTTP_METHOD_LABELS.get(slug, slug)


def operator_label(slug: str) -> str:
    return OPERATOR_LABELS.get(slug, slug)


def action_label(action, fields_by_id: dict[int, Any] | None = None) -> str:
    """e.g. Update → Uptime, Call URL → https://…, Alert → title."""
    at = action.action_type or "?"
    cfg = action.config or {}
    if at == "field_push":
        fid = cfg.get("field_id")
        field = (fields_by_id or {}).get(int(fid)) if fid is not None else None
        if field is not None:
            return f"Update → {field.name}"
        if fid is not None:
            return "Update field"
        return "Update field"
    if at == "http_forward":
        url = (cfg.get("url") or "").strip()
        if url:
            short = url if len(url) <= 48 else url[:45] + "…"
            return f"Call URL → {short}"
        return "Call URL"
    if at == "web_push":
        title = (cfg.get("title") or "").strip()
        return f"Alert → {title}" if title else "Browser notification"
    return action_type_label(at)


def rule_label(rule, event_types_by_id: dict[int, Any] | None = None) -> str:
    """e.g. on on_success, fail — or condition summary."""
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
        parts.append("paused")
    return " · ".join(parts)
