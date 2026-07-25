"""Helpers for global Field storage sinks (logbook / counter / value / toggle)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

FIELD_TYPES = ("logbook", "counter", "value", "toggle")
DEFAULT_MAX_ENTRIES = 100

# Prefix → list index for ``*`` segments (set while a matching rule's actions run).
_star_bindings: ContextVar[dict[str, int] | None] = ContextVar(
    "para_scope_star_bindings", default=None
)


@contextmanager
def path_star_bindings(bindings: dict[str, int] | None):
    """Use matched list indexes for ``*`` in get_by_path (and callers) within the block."""
    token = _star_bindings.set(bindings or None)
    try:
        yield
    finally:
        _star_bindings.reset(token)


def get_by_path(data, path: str, star_bindings: dict[str, int] | None = None):
    """Walk a dotted path into a nested dict/list structure.

    Empty path returns data unchanged. Missing segments return None.
    List segments are integer indexes (e.g. items.0.id) or ``*``.
    With no bindings, ``*`` is index 0. When ``star_bindings`` is passed
    (or set via ``path_star_bindings``), ``*`` under prefix ``value`` uses
    ``star_bindings["value"]`` (empty prefix key ``""`` for a root list).
    Dict keys named ``*`` are looked up normally.
    """
    if not path:
        return data
    if star_bindings is None:
        star_bindings = _star_bindings.get()
    current = data
    walked: list[str] = []
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                if part == "*":
                    prefix = ".".join(walked)
                    idx = (star_bindings or {}).get(prefix, 0)
                    current = current[idx]
                else:
                    current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
        walked.append(part)
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
    raise ValueError("Log entry value isn’t valid")


def resolve_numeric(metric_value, normalized_data: dict | None) -> float:
    """Resolve a literal number or event-data key (dotted path OK) to float."""
    if metric_value is None:
        raise ValueError("A number is required")
    try:
        return float(metric_value)
    except (ValueError, TypeError):
        nd = normalized_data or {}
        raw = get_by_path(nd, metric_value) if isinstance(metric_value, str) else None
        if raw is None:
            raise ValueError(f"Couldn’t find number “{metric_value}” in the event")
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
        raise ValueError("Toggle action needs a value")
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
    raise ValueError("Couldn’t turn that into on/off")
