"""Parse / migrate / merge shared dashboard widget layouts."""
from __future__ import annotations

import json
import secrets
from typing import Any


DEFAULT_W = 6
DEFAULT_H = 3
TABLE_H = 4
COL_FULL = 12

_TALL_TYPES = frozenset({"system", "display", "links"})
_TALL_DISPLAYS = frozenset({
    "source_health", "recent_events", "poller_status", "logbook_list", "table", "list", "board",
})


def new_widget_id() -> str:
    return "w_" + secrets.token_hex(4)


def parse_layout_config(raw) -> dict:
    """Normalize layout_config (str or dict) to {widgets: [...]}."""
    if raw is None:
        return {"widgets": []}
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, TypeError):
            return {"widgets": []}
    elif isinstance(raw, dict):
        data = raw
    else:
        return {"widgets": []}
    widgets = data.get("widgets") if isinstance(data, dict) else None
    if not isinstance(widgets, list):
        widgets = []
    return {"widgets": [w for w in widgets if isinstance(w, dict) and w.get("type")]}


def _default_h(wtype: str, display: str | None = None) -> int:
    if display and display in _TALL_DISPLAYS:
        return TABLE_H
    if wtype in _TALL_TYPES and (not display or display in _TALL_DISPLAYS):
        return TABLE_H
    return DEFAULT_H


def migrate_widgets(widgets: list[dict]) -> tuple[list[dict], bool]:
    """Ensure each widget has id + x/y/w/h. Returns (widgets, changed)."""
    changed = False
    y = 0
    out = []
    for w in widgets:
        item = dict(w)
        legacy = not item.get("id") and all(item.get(k) is None for k in ("x", "y", "w", "h"))
        if not item.get("id"):
            item["id"] = new_widget_id()
            changed = True
        wtype = item.get("type") or ""
        display = item.get("display") or ""
        defaults = {
            "x": 0,
            "y": y,
            "w": COL_FULL if legacy else DEFAULT_W,
            "h": _default_h(wtype, display),
        }
        for key, default in defaults.items():
            if item.get(key) is None:
                item[key] = default
                changed = True
        try:
            item["x"] = int(item["x"])
            item["y"] = int(item["y"])
            item["w"] = max(1, min(COL_FULL, int(item["w"])))
            item["h"] = max(1, int(item["h"]))
        except (TypeError, ValueError):
            item["x"], item["y"] = 0, y
            item["w"] = COL_FULL if legacy else DEFAULT_W
            item["h"] = _default_h(wtype, display)
            changed = True
        if not isinstance(item.get("config"), dict):
            item["config"] = {}
            changed = True
        y = max(y, item["y"] + item["h"])
        out.append(item)
    return out, changed


def layout_json(widgets: list[dict]) -> str:
    return json.dumps({"widgets": widgets})


def merge_geometry(existing: list[dict], updates: list[dict]) -> list[dict]:
    """Merge x/y/w/h from updates onto existing widgets keyed by id."""
    by_id = {
        u["id"]: u
        for u in updates
        if isinstance(u, dict) and u.get("id")
    }
    out = []
    for w in existing:
        item = dict(w)
        uid = item.get("id")
        if uid and uid in by_id:
            u = by_id[uid]
            for key in ("x", "y", "w", "h"):
                if key in u and u[key] is not None:
                    try:
                        item[key] = int(u[key])
                    except (TypeError, ValueError):
                        pass
            if "w" in item:
                item["w"] = max(1, min(COL_FULL, item["w"]))
            if "h" in item:
                item["h"] = max(1, item["h"])
        out.append(item)
    return out


def find_widget(widgets: list[dict], *, widget_id: str | None = None, index: int | None = None) -> dict | None:
    if widget_id:
        for w in widgets:
            if w.get("id") == widget_id:
                return w
    if index is not None and 1 <= index <= len(widgets):
        return widgets[index - 1]
    return None


def normalize_for_save(widgets: list[Any]) -> list[dict]:
    """Normalize widgets from config form; preserve id/geometry; assign missing ids."""
    cleaned = []
    for w in widgets:
        if not isinstance(w, dict) or not w.get("type"):
            continue
        item = {
            "type": w["type"],
            "title": w.get("title") or w.get("label") or w["type"],
            "show_title": bool(w["show_title"]) if "show_title" in w else True,
            "config": w.get("config") if isinstance(w.get("config"), dict) else {},
        }
        if w.get("display"):
            item["display"] = str(w["display"])
        if w.get("id"):
            item["id"] = str(w["id"])
        for key in ("x", "y", "w", "h"):
            if w.get(key) is not None:
                try:
                    item[key] = int(w[key])
                except (TypeError, ValueError):
                    pass
        cleaned.append(item)
    migrated, _ = migrate_widgets(cleaned)
    return migrated
