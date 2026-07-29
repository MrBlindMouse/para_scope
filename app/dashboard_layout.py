"""Parse / merge shared dashboard widget layouts."""
from __future__ import annotations

import json
import secrets
from typing import Any

# Single source of truth for GridStack resolution (JS + CSS read these via the template).
# Design standard ≈ GRID_COLUMNS * GRID_COLUMN_WIDTH px; live cols = round(gridWidth / GRID_COLUMN_WIDTH).
GRID_COLUMNS = 36  # design / saved coordinates (the standard)
GRID_COLUMN_WIDTH = 40  # px per cell; liveCols = round(gridWidth / this)
GRID_CELL_HEIGHT = 40  # px
GRID_MARGIN = 6  # px
# Safety ceiling for CSS + absurd ultrawides (not a second design grid).
GRID_COLUMN_LIVE_MAX = 96
# Layout mode threshold (viewport / window width px); at/below → full-width stacked list.
GRID_STACK_BELOW = 768

DEFAULT_W = max(1, GRID_COLUMNS // 2)
DEFAULT_H = 3
TABLE_H = 4

_TALL_TYPES = frozenset({"system", "display", "links", "notes"})
_TALL_DISPLAYS = frozenset({
    "source_health", "recent_events", "poller_status", "logbook_list", "table", "list", "board",
})


def new_widget_id() -> str:
    return "w_" + secrets.token_hex(4)


def _grid_stack_column_css_n(n: int) -> str:
    """Emit .gs-N width/left percentage rules for a single column count N."""
    pct = 100.0 / n
    parts = [f".gs-{n}>.grid-stack-item{{width:{pct:.4f}%}}"]
    for k in range(1, n + 1):
        parts.append(f'.gs-{n}>.grid-stack-item[gs-w="{k}"]{{width:{pct * k:.4f}%}}')
        if k < n:
            parts.append(f'.gs-{n}>.grid-stack-item[gs-x="{k}"]{{left:{pct * k:.4f}%}}')
    return "".join(parts)


def grid_stack_column_css(columns: int) -> str:
    """Emit .gs-N rules for every N in 1..columns (live column count may shrink)."""
    n = max(1, int(columns))
    return "".join(_grid_stack_column_css_n(i) for i in range(1, n + 1))


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


def _clamp_geometry(item: dict) -> None:
    """Clamp x/w into the live coordinate space (may exceed design GRID_COLUMNS)."""
    item["w"] = max(1, min(GRID_COLUMN_LIVE_MAX, int(item["w"])))
    item["x"] = max(0, min(GRID_COLUMN_LIVE_MAX - 1, int(item["x"])))
    if item["x"] + item["w"] > GRID_COLUMN_LIVE_MAX:
        item["x"] = max(0, GRID_COLUMN_LIVE_MAX - item["w"])
    item["y"] = max(0, int(item["y"]))
    item["h"] = max(1, int(item["h"]))


def normalize_widgets(widgets: list[dict]) -> tuple[list[dict], bool]:
    """Ensure each widget has id + x/y/w/h. Returns (widgets, changed)."""
    changed = False
    y = 0
    out = []
    for w in widgets:
        item = dict(w)
        if not item.get("id"):
            item["id"] = new_widget_id()
            changed = True
        wtype = item.get("type") or ""
        display = item.get("display") or ""
        defaults = {
            "x": 0,
            "y": y,
            "w": DEFAULT_W,
            "h": _default_h(wtype, display),
        }
        for key, default in defaults.items():
            if item.get(key) is None:
                item[key] = default
                changed = True
        try:
            _clamp_geometry(item)
        except (TypeError, ValueError):
            item["x"], item["y"] = 0, y
            item["w"] = DEFAULT_W
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
            try:
                _clamp_geometry(item)
            except (TypeError, ValueError):
                pass
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
        cfg = w.get("config") if isinstance(w.get("config"), dict) else {}
        if w["type"] in ("series", "chart"):
            cfg = {k: v for k, v in cfg.items() if k not in ("tone", "tone_rules")}
        item = {
            "type": w["type"],
            "title": w.get("title") or w["type"],
            "show_title": bool(w["show_title"]) if "show_title" in w else True,
            "config": cfg,
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
    normalized, _ = normalize_widgets(cleaned)
    return normalized
