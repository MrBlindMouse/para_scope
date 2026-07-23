"""Helpers for global Field storage sinks (logbook / counter / value / toggle)."""
from __future__ import annotations

FIELD_TYPES = ("logbook", "counter", "value", "toggle")
DEFAULT_MAX_ENTRIES = 100


def get_by_path(data, path: str):
    """Walk a dotted path into a nested dict/list structure.

    Empty path returns data unchanged. Missing segments return None.
    List segments must be integer indexes (e.g. items.0.id).
    """
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def default_field_config(field_type: str) -> dict:
    if field_type == "logbook":
        return {"max_entries": DEFAULT_MAX_ENTRIES}
    return {}


def default_field_state(field_type: str) -> dict:
    if field_type == "counter":
        return {"value": 0}
    if field_type == "value":
        return {"value": ""}
    if field_type == "toggle":
        return {"value": False}
    return {}


def coerce_logbook_value(raw):
    """Ensure value is JSON-compatible. Raises ValueError on failure."""
    if isinstance(raw, (dict, list, str, int, float, bool)) or raw is None:
        return raw
    raise ValueError(f"logbook value is not JSON-compatible: {type(raw).__name__}")


def resolve_numeric(metric_value, normalized_data: dict | None) -> float:
    """Resolve a literal number or event-data key (dotted path OK) to float."""
    if metric_value is None:
        raise ValueError("numeric value required")
    try:
        return float(metric_value)
    except (ValueError, TypeError):
        nd = normalized_data or {}
        raw = get_by_path(nd, metric_value) if isinstance(metric_value, str) else None
        if raw is None:
            raise ValueError(f"Could not resolve numeric value '{metric_value}'")
        return float(raw)


def resolve_string(config: dict, normalized_data: dict | None) -> str:
    """Resolve literal value or value_key (dotted path OK) to string."""
    nd = normalized_data or {}
    if config.get("value_key"):
        raw = get_by_path(nd, config["value_key"])
        return "" if raw is None else str(raw)
    if "value" in config:
        raw = config["value"]
        return "" if raw is None else str(raw)
    return ""


def resolve_bool(config: dict, normalized_data: dict | None) -> bool:
    """Resolve literal bool or value_key (dotted path OK) to bool."""
    nd = normalized_data or {}
    if config.get("value_key"):
        raw = get_by_path(nd, config["value_key"])
    elif "value" in config:
        raw = config["value"]
    else:
        raise ValueError("toggle requires value or value_key")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return bool(raw)
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
    raise ValueError(f"Could not resolve bool from {raw!r}")
