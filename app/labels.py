"""Display labels for nameless pipeline steps (process → target)."""
from __future__ import annotations

from typing import Any

ACTION_TYPE_LABELS = {
    "field_push": "Update field",
    "http_forward": "Call URL",
    "notify": "Notify",
    "web_push": "Browser notification",
    "local_script": "Local script",
    "trigger_source": "Trigger source",
}

FIELD_TYPE_LABELS = {
    "logbook": "Logbook",
    "value": "Value",
    "text": "Text",
    "toggle": "Toggle",
    "data": "Data",
}

POLL_CATEGORY_LABELS = {
    "url": "HTTP / APIs",
    "system": "Host / OS",
    "connectivity": "Network / DNS / TLS",
    "storage": "Files / Backups",
    "application": "Local Apps / Data",
    "external": "External Services",
}

POLLER_LABELS = {
    "http_get": "GET / HEAD",
    "http_post": "POST",
    "http_put": "PUT",
    "http_delete": "DELETE",
    "system_snapshot": "Host snapshot",
    "systemd_failed_units": "Failed systemd units",
    "journal_recent_errors": "Recent journal errors",
    "tcp_connect": "TCP connect",
    "icmp_ping": "ICMP ping",
    "dns_resolve": "DNS resolve",
    "cert_expiry": "TLS cert expiry",
    "disk_free_space": "Path free space",
    "backup_age": "Backup age",
    "git_status": "Git status",
    "rss_atom_change": "RSS / Atom change",
    "database_health": "Database health",
    "log_pattern_watch": "Log pattern watch",
    "home_assistant_snapshot": "Home Assistant snapshot",
    "imap_unread": "IMAP unread",
    "domain_expiry": "Domain expiry",
    "local_llm_http_status": "Local LLM status",
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


def poller_label(slug: str) -> str:
    return POLLER_LABELS.get(slug, slug)


def poll_category_label(slug: str) -> str:
    return POLL_CATEGORY_LABELS.get(slug, slug)


def http_method_label(slug: str) -> str:
    return poller_label(slug)


def operator_label(slug: str) -> str:
    return OPERATOR_LABELS.get(slug, slug)


def action_label(action, fields_by_id: dict[int, Any] | None = None, sources_by_id: dict[int, Any] | None = None) -> str:
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
    if at == "notify":
        svc = (cfg.get("service") or "").strip() or "notify"
        return f"Notify → {svc}"
    if at == "web_push":
        title = (cfg.get("title") or "").strip()
        return f"Alert → {title}" if title else "Browser notification"
    if at == "local_script":
        argv = cfg.get("argv")
        if isinstance(argv, list) and argv:
            short = str(argv[0])
            if len(short) > 40:
                short = short[:37] + "…"
            return f"Script → {short}"
        cmd = (cfg.get("command") or "").strip()
        if cmd:
            short = cmd if len(cmd) <= 40 else cmd[:37] + "…"
            return f"Script → {short}"
        return "Local script"
    if at == "trigger_source":
        sid = cfg.get("target_source_id")
        src = (sources_by_id or {}).get(int(sid)) if sid is not None else None
        name = src.name if src is not None else (f"#{sid}" if sid is not None else None)
        et_id = cfg.get("event_type_id")
        et_name = None
        if et_id is not None and src is not None:
            for et in getattr(src, "event_types", None) or []:
                if et.id == int(et_id):
                    et_name = et.name
                    break
            if et_name is None:
                et_name = f"#{et_id}"
        if name and et_name:
            return f"Trigger source → {name} / {et_name}"
        if name:
            return f"Trigger source → {name}"
        return "Trigger source"
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
