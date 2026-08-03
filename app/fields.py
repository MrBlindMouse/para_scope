"""Helpers for global Field storage sinks (logbook / value / text / toggle / data)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

FIELD_TYPES = ("logbook", "value", "text", "toggle", "data")
# Names that cannot be Field slugs (enforced by _unique_field_slug).
# field/fields: pipeline reserved; value: conventional state key; source/_poll:
# poll payload injections; ts: series point key; dt/system: reserved for future.
RESERVED_FIELD_SLUGS = ("field", "fields", "value", "source", "_poll", "dt", "system", "ts")
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
    ``**`` is not supported here (use ``collect_by_path``). Dict keys named
    ``*`` / ``**`` are looked up normally.
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
                if part == "**":
                    return None
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


def collect_by_path(data, path: str, star_bindings: dict[str, int] | None = None):
    """Walk a path collecting leaves; ``**`` expands every list index.

    ``*`` is one row (same bindings as ``get_by_path``). Integer indexes and
    dict keys work as usual. Returns a list of leaf values, or None if any
    branch is missing / invalid. Empty ``**`` over an empty list → ``[]``.
    """
    if star_bindings is None:
        star_bindings = _star_bindings.get()
    if not path:
        return [data]

    def walk(current, parts: list[str], walked: list[str]):
        if not parts:
            return [current]
        part, *rest = parts
        if isinstance(current, dict):
            if part not in current:
                return None
            return walk(current[part], rest, walked + [part])
        if isinstance(current, list):
            if part == "**":
                out: list = []
                for item in current:
                    got = walk(item, rest, walked + [part])
                    if got is None:
                        return None
                    out.extend(got)
                return out
            try:
                if part == "*":
                    prefix = ".".join(walked)
                    idx = (star_bindings or {}).get(prefix, 0)
                    return walk(current[idx], rest, walked + [part])
                return walk(current[int(part)], rest, walked + [part])
            except (ValueError, IndexError):
                return None
        return None

    return walk(data, path.split("."), [])


def default_field_config(field_type: str) -> dict:
    if field_type == "logbook":
        return {"max_entries": DEFAULT_MAX_ENTRIES}
    return {}


def default_field_state(field_type: str) -> dict:
    if field_type == "value":
        return {"value": 0}
    if field_type == "text":
        return {"value": ""}
    if field_type == "toggle":
        return {"value": False}
    return {}


def with_current_field(nd: dict | None, current) -> dict:
    """Shallow copy of event data with reserved ``field`` = current stored value."""
    data = dict(nd or {})
    data["field"] = current
    return data


def coerce_logbook_value(raw):
    """Ensure value is JSON-compatible. Raises ValueError on failure."""
    if isinstance(raw, (dict, list, str, int, float, bool)) or raw is None:
        return raw
    raise ValueError("Log entry value isn’t valid")


def coerce_data_value(raw):
    """Ensure data-field value is a JSON object. Raises ValueError on failure."""
    if isinstance(raw, dict):
        return raw
    raise ValueError("Data field value must be a JSON object")


def resolve_numeric(metric_value, normalized_data: dict | None) -> float:
    """Resolve a literal number, event path, or maths expression to float."""
    if metric_value is None:
        raise ValueError("A number is required")
    try:
        return float(metric_value)
    except (ValueError, TypeError):
        pass
    if not isinstance(metric_value, str):
        raise ValueError("A number is required")
    from app.widget_transforms import resolve_path_or_expr

    raw = resolve_path_or_expr(metric_value, normalized_data or {})
    if raw is None:
        raise ValueError(f"Couldn’t find number “{metric_value}” in the event")
    try:
        return float(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Couldn’t find number “{metric_value}” in the event") from e


def resolve_bool(config: dict, normalized_data: dict | None = None) -> bool:
    """Resolve Fixed toggle bool from config value."""
    if "value" not in config:
        raise ValueError("Toggle action needs a value")
    raw = config["value"]
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
    raise ValueError("Value must be on or off")
