"""Numeric transforms for dashboard widget series (path extract + scale ops)."""
from __future__ import annotations

from app.fields import get_by_path


def apply_ops(value, ops: list | None):
    """Apply ordered transform ops to a numeric value. Returns None on failure."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    for step in ops or []:
        if not isinstance(step, dict):
            continue
        op = (step.get("op") or "").strip().lower()
        if op == "abs":
            v = abs(v)
            continue
        if op == "round":
            try:
                digits = int(step.get("digits", 0))
            except (TypeError, ValueError):
                digits = 0
            v = round(v, digits)
            continue
        try:
            by = float(step.get("by", 0))
        except (TypeError, ValueError):
            return None
        if op == "mul":
            v = v * by
        elif op == "div":
            if by == 0:
                return None
            v = v / by
        elif op == "add":
            v = v + by
        elif op == "sub":
            v = v - by
        else:
            continue
    return v


def extract_number(data, value_path: str | None, ops: list | None = None):
    """Resolve a number from data (literal path or whole value) then apply ops."""
    if value_path:
        raw = get_by_path(data, value_path)
    else:
        raw = data
    return apply_ops(raw, ops)


def series_from_points(points, *, value_path: str | None = None, transform: list | None = None):
    """Build [{ts, v}] from iterable of (timestamp, value_payload) pairs."""
    series = []
    for ts, payload in points:
        v = extract_number(payload, value_path, transform)
        if v is None:
            continue
        iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        series.append({"ts": iso, "v": v})
    return series
