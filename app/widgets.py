"""Dashboard widget registry — kinds with selectable display modes."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func as sql_func

from app.widget_transforms import (
    apply_ops,
    extract_number,
    render_data_template,
    resolve_tone_rules,
    series_from_json_array,
    series_from_points,
)

# ── Kind registry ────────────────────────────────────────────────────────────
# ponytail: heatmap / calendar_heatmap / column range — To be implemented (Apex
# grid + min/max pair data shapes; not in KIND_DISPLAYS until then).
# ponytail: notes display "markdown" — To be implemented (render Markdown body;
# keep plain Text as default).

KIND_DISPLAYS = {
    "series": ("line", "area", "column"),
    "chart": ("pie", "radial", "radar", "polar"),
    "display": ("logbook_list", "kv_text", "toggle", "board", "table"),
    "clock": ("digital", "analog", "compact", "world_clock"),
    "links": ("list", "button_row", "icon_grid"),
    "notes": ("notes",),
    "system": ("source_health", "recent_events", "poller_status", "metric_summary"),
}

KIND_TITLES = {
    "series": "Time series",
    "chart": "Chart",
    "display": "Display",
    "clock": "Clock",
    "links": "Links",
    "notes": "Notes",
    "system": "System",
}

DISPLAY_TITLES = {
    "line": "Line",
    "area": "Area",
    "column": "Column",
    "pie": "Pie / Donut",
    "radial": "Radial / Gauge",
    "radar": "Radar",
    "polar": "Polar Area",
    "logbook_list": "Logbook list",
    "kv_text": "Key / text",
    "toggle": "Toggle",
    "board": "Board",
    "table": "Table",
    "digital": "Digital",
    "analog": "Analog",
    "compact": "Compact",
    "world_clock": "World clocks",
    "list": "List",
    "button_row": "Button row",
    "icon_grid": "Icon grid",
    "notes": "Text",
    "source_health": "Source Health",
    "recent_events": "Recent Events",
    "poller_status": "Poll status",
    "metric_summary": "Metric Summary",
}

# Visual style variants per display mode (config.style)
DISPLAY_STYLES = {
    "toggle": ("text_color", "led", "badge", "switch"),
    "logbook_list": ("code", "timeline", "cards"),
    "kv_text": ("plain", "mono", "callout"),
    "table": ("plain", "compact", "striped"),
    "digital": ("plain", "mono", "callout"),
    "analog": ("plain", "ring"),
    "compact": ("plain", "mono"),
    "world_clock": ("list", "cards"),
    "line": ("basic", "labels", "multi", "stepline"),
    "area": ("basic", "negative", "stacked"),
    "column": ("basic", "labels", "stacked", "stacked_100", "negative"),
    "pie": ("pie", "donut"),
    "radial": (
        "basic", "multi_band", "custom_angle", "gradient",
        "stroked_gauge", "gauge_ticks", "needle",
    ),
    "radar": ("basic",),
    "polar": ("basic",),
    "list": ("default", "compact", "emphasized"),
    "button_row": ("default", "compact", "emphasized"),
    "icon_grid": ("default", "compact", "emphasized"),
    "source_health": ("table", "compact", "cards"),
    "recent_events": ("table", "compact", "cards"),
    "poller_status": ("table", "compact", "cards"),
    "metric_summary": ("table", "compact", "cards"),
}

STYLE_TITLES = {
    "text_color": "On/Off text",
    "led": "Green/Red light",
    "badge": "Badge",
    "switch": "Switch",
    "plain": "Plain",
    "compact": "Compact",
    "code": "Code list",
    "timeline": "Timeline",
    "cards": "Cards",
    "mono": "Monospace",
    "callout": "Callout",
    "striped": "Striped",
    "default": "Default",
    "basic": "Basic",
    "labels": "With labels",
    "multi": "Multi-series",
    "stepline": "Stepline",
    "negative": "Negative values",
    "stacked": "Stacked",
    "stacked_100": "Stacked 100%",
    "pie": "Pie",
    "donut": "Donut",
    "multi_band": "Multiple bands",
    "custom_angle": "Custom angle",
    "gradient": "Gradient",
    "stroked_gauge": "Stroked gauge",
    "gauge_ticks": "Gauge with ticks",
    "needle": "Needle gauge",
    "emphasized": "Emphasized",
    "table": "Table",
    "ring": "Ring face",
}

# Extra config keys + source cardinality per (display, style).
# keys: allowlisted style-specific config fields (plus shared sources/unit/range).
STYLE_CONFIG: dict[tuple[str, str], dict] = {
    ("line", "basic"): {"keys": (), "min_sources": 1},
    ("line", "labels"): {"keys": (), "min_sources": 1},
    ("line", "multi"): {"keys": (), "min_sources": 2},
    ("line", "stepline"): {"keys": (), "min_sources": 1},
    ("area", "basic"): {"keys": (), "min_sources": 1},
    ("area", "negative"): {"keys": (), "min_sources": 1},
    ("area", "stacked"): {"keys": (), "min_sources": 2},
    ("column", "basic"): {"keys": ("horizontal",), "min_sources": 1},
    ("column", "labels"): {"keys": ("horizontal",), "min_sources": 1},
    ("column", "stacked"): {"keys": ("horizontal",), "min_sources": 2},
    ("column", "stacked_100"): {"keys": ("horizontal",), "min_sources": 2},
    ("column", "negative"): {"keys": ("horizontal",), "min_sources": 1},
    ("pie", "pie"): {"keys": (), "min_sources": 1},
    ("pie", "donut"): {"keys": (), "min_sources": 1},
    ("radial", "basic"): {"keys": ("max", "max_field_slug"), "min_sources": 1, "max_sources": 1},
    ("radial", "multi_band"): {"keys": ("max", "max_field_slug"), "min_sources": 2},
    ("radial", "custom_angle"): {
        "keys": ("max", "max_field_slug", "start_angle", "end_angle"),
        "min_sources": 2,
    },
    ("radial", "gradient"): {"keys": ("max", "max_field_slug"), "min_sources": 1, "max_sources": 1},
    ("radial", "stroked_gauge"): {
        "keys": ("max", "max_field_slug"), "min_sources": 1, "max_sources": 1,
    },
    ("radial", "gauge_ticks"): {
        "keys": ("max", "max_field_slug"), "min_sources": 1, "max_sources": 1,
    },
    ("radial", "needle"): {"keys": ("max", "max_field_slug"), "min_sources": 1, "max_sources": 1},
    ("radar", "basic"): {"keys": (), "min_sources": 3},
    ("polar", "basic"): {"keys": (), "min_sources": 3},
}


def style_config_for(display: str, style: str) -> dict:
    """Return STYLE_CONFIG entry for display+style (empty dict if none)."""
    return dict(STYLE_CONFIG.get((display, style)) or {})


def style_extra_keys(display: str, style: str) -> tuple:
    return tuple(style_config_for(display, style).get("keys") or ())

# Field binding rules: kind-level, with optional per-display overrides
_BINDING_SERIES = {
    "cardinality": "multi",
    "field_types": ("logbook", "data"),
    "config_key": "sources",
    "required": True,
}
_BINDING_CHART = {
    "cardinality": "multi",
    "field_types": ("value", "text", "logbook", "data"),
    "config_key": "sources",
    "required": True,
}
_BINDING_NONE = {"cardinality": "none", "field_types": (), "config_key": None, "required": False}

WIDGET_BINDINGS = {
    "series": _BINDING_SERIES,
    "chart": _BINDING_CHART,
    "display": {
        "by_display": {
            "logbook_list": {
                "cardinality": "single",
                "field_types": ("logbook",),
                "config_key": "field_slug",  # or template with {{ slug… }}
                "required": True,
            },
            "kv_text": {
                "cardinality": "none",
                "field_types": (),
                "config_key": None,
                "required": False,
            },
            "toggle": {
                "cardinality": "single",
                "field_types": ("toggle",),
                "config_key": "field_slug",
                "required": True,
            },
            "board": {
                "cardinality": "multi",
                "field_types": None,  # resolved from cell_kind at validate time
                "config_key": "cells",
                "required": True,
            },
            "table": {
                "cardinality": "multi",
                "field_types": None,
                "config_key": "field_slugs",
                "required": True,
            },
        },
    },
    "clock": _BINDING_NONE,
    "links": _BINDING_NONE,
    "notes": _BINDING_NONE,
    "system": _BINDING_NONE,
}

_TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def binding_for(kind: str, display: str | None = None) -> dict:
    """Resolve Field-binding rules for a widget kind (+ display)."""
    raw = WIDGET_BINDINGS.get(kind) or _BINDING_NONE
    by = raw.get("by_display") if isinstance(raw, dict) else None
    if by:
        disp = (display or "").strip() or default_display(kind)
        return dict(by.get(disp) or _BINDING_NONE)
    return dict(raw)


def default_style(display: str) -> str:
    styles = DISPLAY_STYLES.get(display) or ()
    return styles[0] if styles else "default"


def styles_for(display: str) -> list[dict]:
    return [
        {"id": s, "title": STYLE_TITLES.get(s, s)}
        for s in (DISPLAY_STYLES.get(display) or ())
    ]


def get_widget_kinds():
    """Return catalog entries for the config UI."""
    out = []
    for kind, displays in KIND_DISPLAYS.items():
        disp_entries = []
        for d in displays:
            styles = styles_for(d)
            for s in styles:
                sc = style_config_for(d, s["id"])
                s["keys"] = list(sc.get("keys") or ())
                s["min_sources"] = int(sc.get("min_sources") or 1)
                if "max_sources" in sc:
                    s["max_sources"] = int(sc["max_sources"])
            disp_entries.append({
                "id": d,
                "title": DISPLAY_TITLES.get(d, d),
                "styles": styles,
                "default_style": default_style(d),
                "binding": binding_for(kind, d),
            })
        out.append({
            "type": kind,
            "title": KIND_TITLES[kind],
            "displays": disp_entries,
            "default_display": displays[0],
            "binding": binding_for(kind, displays[0]),
            "template": f"widgets/{kind}_content.html",
        })
    return out


def get_widget_types():
    """Alias used by routes — kinds catalog."""
    return get_widget_kinds()


def default_display(kind: str) -> str:
    displays = KIND_DISPLAYS.get(kind) or ()
    return displays[0] if displays else ""


def resolve_style(config: dict | None, display: str) -> str:
    cfg = config or {}
    raw = (cfg.get("style") or "").strip()
    if display == "board":
        allowed = DISPLAY_STYLES.get(_board_cell_kind(cfg)) or ()
    else:
        allowed = DISPLAY_STYLES.get(display) or ()
    if raw and raw in allowed:
        return raw
    return allowed[0] if allowed else "default"


WIDGET_TONES = ("none", "conditional")


BOARD_CELL_KINDS = ("toggle", "kv_text")
BOARD_CELL_FIELD_TYPES = {
    "toggle": ("toggle",),
    "kv_text": None,  # any
}

CLOCK_TIMEZONE_MODES = ("app", "browser", "utc", "custom")
CLOCK_HOUR_FORMATS = ("12", "24")
CLOCK_LAYOUTS = ("column", "row")
CLOCK_WORLD_CLOCK_LIMIT = 8


def default_tone(widget_type: str, display: str) -> str:
    """conditional for toggle/board/kv_text/logbook_list; none elsewhere."""
    if widget_type == "display" and display in ("toggle", "board", "kv_text", "logbook_list"):
        return "conditional"
    return "none"


def _config_tone_rules(config: dict | None) -> list:
    raw = (config or {}).get("tone_rules")
    return raw if isinstance(raw, list) else []


def _cell_tone(item: dict) -> str:
    kind = item.get("kind")
    if kind == "toggle":
        return "positive" if item.get("value") else "negative"
    if kind == "kv_text":
        return item.get("tone") or "neutral"
    return "neutral"


def resolve_widget_tone(
    config: dict | None,
    *,
    widget_type: str,
    display: str,
    data: dict | None = None,
) -> str | None:
    """Return positive|negative|neutral from input, or None when no background.

    Board with kv_text cells uses per-row tones only (no widget wrapper tone).
    Series/chart never use rule-based backgrounds.
    """
    if widget_type in ("series", "chart"):
        return None
    cfg = config or {}
    raw = (cfg.get("tone") or "").strip().lower()
    if raw not in WIDGET_TONES:
        raw = default_tone(widget_type, display)
    if raw == "none":
        return None
    data = data or {}
    if display == "toggle":
        return "positive" if data.get("value") else "negative"
    if display in ("kv_text", "logbook_list", "notes"):
        return resolve_tone_rules(
            _config_tone_rules(cfg), data.get("_tone_data") or {},
        )
    if display == "board":
        cell_kind = _board_cell_kind(cfg)
        if cell_kind == "kv_text":
            # Per-row tones on items; no whole-widget background.
            return None
        items = data.get("items") or []
        if not items:
            return "neutral"
        tones = [_cell_tone(it) for it in items]
        if all(t == "positive" for t in tones):
            return "positive"
        if all(t == "negative" for t in tones):
            return "negative"
        return "neutral"
    return None


def widget_referenced_field_ids(db, config: dict | None) -> set[int]:
    """Field PKs referenced by a widget config (slugs on config / sources / cells)."""
    from app.models import Field

    cfg = config or {}
    slugs: set[str] = set()

    def _add(raw: str):
        slug, _ = split_slug_path(raw)
        if slug:
            slugs.add(slug)

    _add(_config_field_slug(cfg))
    _add((cfg.get("max_field_slug") or "").strip())
    for part in _config_field_slugs(cfg):
        _add(part)
    tmpl = (cfg.get("template") or "").strip()
    if "{{" in tmpl:
        for head in _slug_heads_from_template(tmpl):
            _add(head)
    sources = cfg.get("sources")
    if isinstance(sources, list):
        for src in sources:
            if isinstance(src, dict):
                _add((src.get("field_slug") or "").strip())
    cells = cfg.get("cells")
    if isinstance(cells, list):
        for cell in cells:
            if isinstance(cell, dict):
                _add((cell.get("field_slug") or "").strip())
                ct = (cell.get("template") or "").strip()
                if "{{" in ct:
                    for head in _slug_heads_from_template(ct):
                        _add(head)
    if not slugs:
        return set()
    rows = db.query(Field.id).filter(Field.slug.in_(slugs)).all()
    return {r[0] for r in rows}


def validate_widget_bindings(db, widgets: list) -> str | None:
    """Return error message if any widget violates Field binding rules."""
    from app.models import Field

    fields_by_slug = {f.slug: f for f in db.query(Field).all() if f.slug}
    for i, w in enumerate(widgets):
        if not isinstance(w, dict):
            continue
        kind = (w.get("type") or "").strip()
        if kind not in KIND_DISPLAYS:
            return f"Widget {i + 1}: unknown widget type"
        disp = (w.get("display") or default_display(kind) or "").strip()
        cfg = w.get("config") if isinstance(w.get("config"), dict) else {}
        label = f"Widget {i + 1} ({KIND_TITLES.get(kind, kind)} / {DISPLAY_TITLES.get(disp, disp)})"
        if disp not in (KIND_DISPLAYS.get(kind) or ()):
            return f"{label}: that display isn’t available"
        rule = binding_for(kind, disp)
        card = rule.get("cardinality") or "none"
        style = resolve_style(cfg, disp)
        board_kind = _board_cell_kind(cfg) if disp == "board" else ""
        style_allowed = (
            DISPLAY_STYLES.get(board_kind) if disp == "board" else DISPLAY_STYLES.get(disp)
        ) or ()
        raw_style = (cfg.get("style") or "").strip()
        if raw_style and raw_style not in style_allowed:
            return f"{label}: that style isn’t available"
        sc = style_config_for(disp, style)
        if kind == "clock":
            err = _validate_clock_widget(label, disp, cfg)
            if err:
                return f"{label}: {err}"
        if card == "none":
            continue
        allowed = rule.get("field_types")  # None = any
        if disp == "board":
            allowed = BOARD_CELL_FIELD_TYPES.get(board_kind)
        required = bool(rule.get("required"))
        key = rule.get("config_key")

        if disp == "logbook_list":
            field, err = _resolve_logbook_list_field(db, cfg, fields_by_slug)
            if err:
                return f"{label}: {err}"
            if field is None:
                if required:
                    return f"{label}: choose a field"
                continue
            if allowed is not None and field.field_type not in allowed:
                return f"{label}: “{field.name}” isn’t a compatible field type"
            continue

        if key == "sources":
            sources = cfg.get("sources") if isinstance(cfg.get("sources"), list) else []
            filled = [
                s for s in sources
                if isinstance(s, dict) and (s.get("field_slug") or "").strip()
            ]
            if required and not filled:
                return f"{label}: pick at least one field"
            min_src = int(sc.get("min_sources") or 1)
            max_src = sc.get("max_sources")
            if filled and len(filled) < min_src:
                return f"{label}: needs at least {min_src} field(s) for this style"
            if max_src is not None and len(filled) > int(max_src):
                return f"{label}: use at most {max_src} field(s) for this style"
            for src in filled:
                raw = (src.get("field_slug") or "").strip()
                slug, path = split_slug_path(raw)
                field = fields_by_slug.get(slug)
                if field is None:
                    return f"{label}: that field wasn’t found"
                if allowed is not None and field.field_type not in allowed:
                    return f"{label}: “{field.name}” isn’t the right field type"
                if field.field_type in ("logbook", "data") and not path:
                    return f"{label}: append a path (e.g. {slug}.response_time_ms)"
            continue

        if key == "cells":
            cells = _board_cells(cfg)
            if required and not cells:
                return f"{label}: pick at least one field"
            for cell in cells:
                slug_raw = (cell.get("field_slug") or "").strip()
                if not slug_raw:
                    if board_kind == "kv_text":
                        continue  # template-only entry
                    return f"{label}: pick at least one field"
                slug, _ = split_slug_path(slug_raw)
                field = fields_by_slug.get(slug)
                if field is None:
                    return f"{label}: that field wasn’t found"
                if allowed is not None and field.field_type not in allowed:
                    return f"{label}: “{field.name}” isn’t the right field type"
            continue

        if key == "field_slugs":
            slugs = _config_field_slugs(cfg)
            if required and not slugs:
                return f"{label}: pick at least one field"
            for s in slugs:
                slug, _ = split_slug_path(s)
                field = fields_by_slug.get(slug)
                if not field:
                    return f"{label}: that field wasn’t found"
                if allowed is not None and field.field_type not in allowed:
                    return f"{label}: “{field.name}” isn’t the right field type"
            continue

        # single field_slug
        slug_raw = _config_field_slug(cfg)
        if not slug_raw:
            if required:
                return f"{label}: choose a field"
            continue
        slug, _ = split_slug_path(slug_raw)
        field = fields_by_slug.get(slug)
        if not field:
            return f"{label}: that field wasn’t found"
        if allowed is not None and field.field_type not in allowed:
            return f"{label}: “{field.name}” isn’t a compatible field type"
    return None


def fetch_widget_data(widget_type, db, widget_config=None, source_id=None, display=None):
    """Fetch data for a widget kind (+ optional display mode)."""
    config = dict(widget_config or {})
    disp = (display or config.get("display") or default_display(widget_type) or "").strip()
    fn = {
        "series": _series_data,
        "chart": _chart_data,
        "display": _display_data,
        "clock": _clock_data,
        "links": _links_data,
        "notes": _notes_data,
        "system": _system_data,
    }.get(widget_type)
    if not fn:
        return {"error": "Unknown widget"}
    snap = fields_snapshot(db)
    data = fn(db, config, display=disp, source_id=source_id, fields_snap=snap)
    if isinstance(data, dict):
        data.setdefault("display", disp)
        data["style"] = resolve_style(config, disp)
        tone = resolve_widget_tone(
            config, widget_type=widget_type, display=disp, data=data,
        )
        if tone:
            data["tone"] = tone
    return data


def _config_field_slug(config) -> str:
    return (config.get("field_slug") or "").strip() if isinstance(config, dict) else ""


def split_slug_path(raw: str) -> tuple[str, str | None]:
    """Split `fieldslug.path.to.value` → (field slug, path or None)."""
    s = (raw or "").strip()
    if not s:
        return "", None
    if "." not in s:
        return s, None
    slug, path = s.split(".", 1)
    slug = slug.strip()
    path = path.strip()
    return slug, path or None


def _slug_heads_from_template(template: str) -> list[str]:
    """First identifier in each ``{{ … }}`` body (slug head)."""
    heads: list[str] = []
    for m in _TEMPLATE_RE.finditer(template or ""):
        body = m.group(1).strip()
        im = re.match(r"[a-zA-Z_][a-zA-Z0-9_]*", body)
        if im:
            heads.append(im.group(0))
    return heads


def _resolve_logbook_list_field(db, config, fields_by_slug: dict | None = None):
    """Return (Field|None, error|None) for logbook_list field_slug or template."""
    from app.models import Field

    cfg = config if isinstance(config, dict) else {}
    template = (cfg.get("template") or "").strip()
    slug = _config_field_slug(cfg)

    if template and "{{" in template:
        if fields_by_slug is None:
            fields_by_slug = {f.slug: f for f in db.query(Field).all() if f.slug}
        logbooks = []
        seen = set()
        for head in _slug_heads_from_template(template):
            f = fields_by_slug.get(head)
            if f is None or f.field_type != "logbook" or f.id in seen:
                continue
            seen.add(f.id)
            logbooks.append(f)
        if not logbooks:
            return None, "template must reference a logbook Field"
        if len(logbooks) > 1:
            return None, "template must reference exactly one logbook Field"
        return logbooks[0], None

    if not slug:
        return None, "choose a field"
    if fields_by_slug is not None:
        field = fields_by_slug.get(slug)
    else:
        field = db.query(Field).filter(Field.slug == slug).first()
    if field is None:
        return None, "that field wasn’t found"
    if field.field_type != "logbook":
        return None, "This widget needs a Logbook field"
    return field, None


def resolve_field(db, config) -> object | None:
    """Resolve a Field by field_slug (path after first `.` is ignored)."""
    from app.models import Field

    slug, _ = split_slug_path(_config_field_slug(config))
    if not slug:
        return None
    return db.query(Field).filter(Field.slug == slug).first()


def fields_snapshot(db) -> dict:
    """All Fields keyed by slug → state or latest logbook entry payload.

    ponytail: loads all log entries then keeps first-per-field; fine at small
    Field counts — upgrade to a window/GROUP BY if this gets hot.
    """
    from app.models import Field, FieldLogEntry

    fields = db.query(Field).all()
    logbook_ids = [f.id for f in fields if f.field_type == "logbook"]
    latest: dict[int, object] = {}
    if logbook_ids:
        entries = (
            db.query(FieldLogEntry)
            .filter(FieldLogEntry.field_id.in_(logbook_ids))
            .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
            .all()
        )
        for e in entries:
            if e.field_id not in latest:
                latest[e.field_id] = e.value
    snap: dict = {}
    for f in fields:
        slug = (f.slug or "").strip()
        if not slug:
            continue
        if f.field_type == "logbook":
            val = latest.get(f.id)
            if isinstance(val, dict):
                snap[slug] = val
            elif val is not None:
                snap[slug] = {"value": val}
            else:
                snap[slug] = {}
        else:
            snap[slug] = dict(f.state or {})
    return snap


def _merge_template_data(snap: dict | None, bound: dict | None) -> dict:
    """Global slug namespace under snap; bound Field data wins on key clash."""
    return {**(snap or {}), **(bound or {})}


def _config_field_slugs(config) -> list[str]:
    raw = config.get("field_slugs") if isinstance(config, dict) else None
    if isinstance(raw, str) and raw.strip():
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if str(s).strip()]
    return []


def _config_transform(config) -> list:
    raw = config.get("transform")
    return raw if isinstance(raw, list) else []


def _board_cell_kind(config: dict) -> str:
    """Board cell type from config.cell_kind."""
    kind = (config.get("cell_kind") or "").strip()
    if kind in BOARD_CELL_KINDS:
        return kind
    return "toggle"


def _board_cells(config: dict) -> list[dict]:
    """Board entry rows: toggle needs field_slug; kv_text needs template (optional slug)."""
    kind = _board_cell_kind(config)
    cells_raw = config.get("cells")
    if not isinstance(cells_raw, list):
        return []
    out = []
    for c in cells_raw:
        if not isinstance(c, dict):
            continue
        slug = (c.get("field_slug") or "").strip()
        template = c.get("template") or ""
        if kind == "kv_text":
            if not (template or "").strip():
                continue
        elif not slug:
            continue
        t = c.get("transform")
        rules = c.get("tone_rules")
        out.append({
            "field_slug": slug,
            "transform": t if isinstance(t, list) else [],
            "unit": (c.get("unit") or "").strip() if isinstance(c.get("unit"), str) else "",
            "template": template,
            "tone_rules": rules if isinstance(rules, list) else [],
        })
    return out


def _range_hours(config, default=24) -> int:
    try:
        range_hours = int(config.get("range_hours", default))
    except (TypeError, ValueError):
        range_hours = default
    return max(1, min(range_hours, 8760))


def _range_mode(config) -> str:
    mode = (config.get("range_mode") or "hours").strip().lower()
    return mode if mode in ("hours", "entries") else "hours"


def _range_entries(config, default=50) -> int:
    return _int_limit(config, "range_entries", default, lo=1, hi=1000)


def _int_limit(config, key, default, lo=1, hi=100) -> int:
    try:
        n = int(config.get(key, default))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _bool_config(config: dict | None, key: str, default: bool = False) -> bool:
    if not isinstance(config, dict) or key not in config:
        return default
    value = config.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off", ""):
            return False
    return bool(value)


def _clock_timezone_name(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("enter an IANA timezone like Africa/Johannesburg")
    try:
        return ZoneInfo(value).key
    except ZoneInfoNotFoundError as exc:
        raise ValueError("enter a valid IANA timezone like Africa/Johannesburg") from exc


def _clock_timezone_mode(config: dict | None) -> str:
    raw = (config.get("timezone_mode") or "").strip().lower() if isinstance(config, dict) else ""
    return raw if raw in CLOCK_TIMEZONE_MODES else "app"


def _clock_hour_format(config: dict | None) -> str:
    raw = (config.get("hour_format") or "").strip() if isinstance(config, dict) else ""
    return raw if raw in CLOCK_HOUR_FORMATS else "24"


def _clock_layout(config: dict | None) -> str:
    raw = (config.get("layout") or "").strip().lower() if isinstance(config, dict) else ""
    return raw if raw in CLOCK_LAYOUTS else "column"


def _clock_world_clocks(config: dict | None) -> list[dict]:
    rows = config.get("world_clocks") if isinstance(config, dict) else None
    if not isinstance(rows, list):
        return []
    cleaned = []
    for row in rows[:CLOCK_WORLD_CLOCK_LIMIT]:
        if not isinstance(row, dict):
            continue
        label = (row.get("label") or "").strip()
        timezone_name = (row.get("timezone") or "").strip()
        if not (label or timezone_name):
            continue
        cleaned.append({"label": label, "timezone": timezone_name})
    return cleaned


def _clock_app_timezone(db) -> str:
    from app.themes import get_display_timezone

    return get_display_timezone(db)


def _clock_resolved_timezone(db, config: dict | None) -> tuple[str, str | None]:
    mode = _clock_timezone_mode(config)
    if mode == "utc":
        return mode, "UTC"
    if mode == "custom":
        return mode, _clock_timezone_name((config or {}).get("timezone"))
    return mode, _clock_app_timezone(db)


def _clock_label(label: str | None, timezone_name: str, fallback: str) -> str:
    return (label or "").strip() or timezone_name or fallback


def _clock_offset_label(value: datetime) -> str:
    offset = value.utcoffset() or timedelta()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _clock_time_text(value: datetime, *, show_seconds: bool, hour_format: str) -> str:
    fmt = "%I:%M:%S %p" if hour_format == "12" and show_seconds else (
        "%I:%M %p" if hour_format == "12" else "%H:%M:%S" if show_seconds else "%H:%M"
    )
    text = value.strftime(fmt)
    return text[1:] if hour_format == "12" and text.startswith("0") else text


def _clock_face_payload(
    *,
    label: str,
    timezone_name: str,
    now_utc: datetime,
    show_seconds: bool,
    show_date: bool,
    hour_format: str,
) -> dict:
    current = now_utc.astimezone(ZoneInfo(timezone_name))
    hour = current.hour % 12
    minute = current.minute
    second = current.second
    return {
        "label": label,
        "timezone": timezone_name,
        "time_text": _clock_time_text(current, show_seconds=show_seconds, hour_format=hour_format),
        "date_text": current.strftime("%a %d %b %Y") if show_date else "",
        "day_text": current.strftime("%A"),
        "offset_text": _clock_offset_label(current),
        "hour_angle": (hour + (minute / 60.0) + (second / 3600.0)) * 30.0,
        "minute_angle": (minute + (second / 60.0)) * 6.0,
        "second_angle": second * 6.0,
    }


def _validate_clock_widget(label: str, display: str, config: dict | None) -> str | None:
    cfg = config or {}
    mode = _clock_timezone_mode(cfg)
    raw_mode = (cfg.get("timezone_mode") or "").strip().lower()
    if raw_mode and raw_mode not in CLOCK_TIMEZONE_MODES:
        return "that timezone mode isn’t available"
    if mode == "custom":
        try:
            _clock_timezone_name(cfg.get("timezone"))
        except ValueError as exc:
            return str(exc)
    elif (cfg.get("timezone") or "").strip() and mode != "custom":
        return "custom timezone only applies when timezone mode is Custom"
    raw_hour = (cfg.get("hour_format") or "").strip()
    if raw_hour and raw_hour not in CLOCK_HOUR_FORMATS:
        return "that hour format isn’t available"
    for key in ("show_seconds", "show_date", "show_timezone"):
        raw = cfg.get(key)
        if raw is not None and not isinstance(raw, (bool, int, str)):
            return f"{key.replace('_', ' ')} must be on or off"
    raw_layout = (cfg.get("layout") or "").strip().lower()
    if raw_layout and raw_layout not in CLOCK_LAYOUTS:
        return "that layout isn’t available"
    if raw_layout and display != "world_clock":
        return "row/column layout only applies to world clocks"
    world_clocks = _clock_world_clocks(cfg)
    if display == "world_clock":
        if not world_clocks:
            return "add at least one world clock"
        for row in world_clocks:
            if not row["label"].strip():
                return "world clocks need a label"
            try:
                _clock_timezone_name(row["timezone"])
            except ValueError as exc:
                return f"world clock “{row['label']}”: {exc}"
    elif world_clocks:
        for row in world_clocks:
            try:
                _clock_timezone_name(row["timezone"])
            except ValueError as exc:
                return f"world clock “{row['label'] or 'clock'}”: {exc}"
    return None


# ── series ───────────────────────────────────────────────────────────────────

def _series_source_rows(config) -> list[dict]:
    """Series sources from config.sources[] (field_slug required per row)."""
    sources = config.get("sources")
    if not isinstance(sources, list):
        return []
    return [
        s for s in sources
        if isinstance(s, dict) and (s.get("field_slug") or "").strip()
    ]


def _series_points_for_source(db, src, *, range_mode, range_hours, range_entries, cutoff, source_id=None):
    """Return (name, points[{ts,v}]) for one series source row."""
    from app.models import FieldLogEntry

    transform = src.get("transform") if isinstance(src.get("transform"), list) else []
    label = (src.get("label") or "").strip()

    def _apply_range(query, ts_col, id_col):
        if range_mode == "entries":
            rows = (
                query.order_by(ts_col.desc(), id_col.desc())
                .limit(range_entries)
                .all()
            )
            rows.reverse()
            return rows
        return query.filter(ts_col >= cutoff).order_by(ts_col).all()

    field = resolve_field(db, src)
    if field is None:
        return None, [], "Choose a field"

    name = label or field.name
    field_id = field.id
    _, value_path = split_slug_path(_config_field_slug(src))

    if field.field_type == "logbook":
        if not value_path:
            return name, [], "Append a path (e.g. field._poll.response_time_ms)"
        q = db.query(FieldLogEntry).filter(FieldLogEntry.field_id == field_id)
        if source_id:
            q = q.filter(FieldLogEntry.source_id == source_id)
        rows = _apply_range(q, FieldLogEntry.timestamp, FieldLogEntry.id)
        pairs = [(e.timestamp, e.value) for e in rows]
        series = series_from_points(pairs, value_path=value_path, transform=transform)
        return name, series, None

    if field.field_type == "data":
        if not value_path:
            return name, [], "Append a path (e.g. field.samples.*.ms)"
        state = field.state if isinstance(field.state, dict) else {}
        series, err = series_from_json_array(
            state,
            value_path,
            transform=transform,
            range_mode=range_mode,
            range_entries=range_entries,
            cutoff=cutoff,
        )
        return name, series, err

    return name, [], "series requires a logbook or Data Field"


def _series_data(db, config, display="line", source_id=None, fields_snap=None):
    range_mode = _range_mode(config)
    range_hours = _range_hours(config)
    range_entries = _range_entries(config)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=range_hours)
    unit = (config.get("unit") or "").strip()
    base = {
        "display": display,
        "range_mode": range_mode,
        "range_hours": range_hours,
        "range_entries": range_entries,
        "unit": unit,
        "series": [],
    }
    rows = _series_source_rows(config)
    if not rows:
        return {**base, "error": "Choose at least one field"}

    series_out = []
    errors = []
    for src in rows:
        name, points, err = _series_points_for_source(
            db, src,
            range_mode=range_mode,
            range_hours=range_hours,
            range_entries=range_entries,
            cutoff=cutoff,
            source_id=source_id,
        )
        if err and not points:
            errors.append(err)
            continue
        series_out.append({"name": name or "", "points": points})

    if not series_out:
        return {**base, "error": errors[0] if errors else "No data yet"}

    # Convenience: single-series name at top level (legacy tests / UI)
    name = series_out[0]["name"] if len(series_out) == 1 else ""
    out = {**base, "series": series_out, "name": name}
    if display == "column":
        out["horizontal"] = bool(config.get("horizontal"))
    return out


# ── chart (value / text slices) ──────────────────────────────────────────────

def _latest_logbook_entry_value(db, field, source_id=None):
    from app.models import FieldLogEntry

    q = db.query(FieldLogEntry).filter(FieldLogEntry.field_id == field.id)
    if source_id:
        q = q.filter(FieldLogEntry.source_id == source_id)
    entry = q.order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc()).first()
    return None if entry is None else entry.value


def _latest_chart_source_payload(db, field, *, source_id=None):
    if field.field_type == "logbook":
        return _latest_logbook_entry_value(db, field, source_id=source_id)
    if field.field_type == "data":
        return field.state if isinstance(field.state, dict) else {}
    state = field.state or {}
    return state.get("value", 0 if field.field_type == "value" else "")


def _chart_source_value(db, src, source_id=None):
    """Return (label, numeric_value) for one chart source."""
    label = (src.get("label") or "").strip()
    field = resolve_field(db, src)
    if field is None:
        return None
    if not label:
        label = field.name
    if field.field_type not in ("value", "text", "logbook", "data"):
        return None
    transform = src.get("transform") if isinstance(src.get("transform"), list) else []
    _, value_path = split_slug_path(_config_field_slug(src))
    raw = _latest_chart_source_payload(db, field, source_id=source_id)
    if field.field_type == "value":
        v = apply_ops(raw, transform) if transform else float(raw or 0)
        return label, 0.0 if v is None else v
    if field.field_type == "text":
        v = apply_ops(raw, transform) if transform else None
        if v is None:
            try:
                v = float(raw)
            except (TypeError, ValueError):
                v = 0.0
        return label, v
    v = extract_number(raw, value_path, transform)
    return label, 0.0 if v is None else v


def _chart_max(db, config) -> float:
    """Explicit max/target: config.max number, else max_field_slug Field, else 100."""
    if not isinstance(config, dict):
        return 100.0
    raw = config.get("max")
    if raw is not None and raw != "":
        try:
            m = float(raw)
            if m > 0:
                return m
        except (TypeError, ValueError):
            pass
    slug = (config.get("max_field_slug") or "").strip()
    if slug:
        pair = _chart_source_value(db, {"field_slug": slug})
        if pair is not None:
            try:
                m = float(pair[1])
            except (TypeError, ValueError):
                m = 0.0
            if m > 0:
                return m
    return 100.0


def _chart_angle(config, key: str, default: float) -> float:
    raw = config.get(key) if isinstance(config, dict) else None
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _chart_data(db, config, display="pie", source_id=None, fields_snap=None):
    sources = config.get("sources")
    if not isinstance(sources, list):
        sources = []
    labels, values = [], []
    for src in sources:
        if not isinstance(src, dict):
            continue
        pair = _chart_source_value(db, src, source_id=source_id)
        if pair is None:
            continue
        labels.append(pair[0])
        values.append(pair[1])
    style = resolve_style(config, display)
    sc = style_config_for(display, style)
    out = {
        "display": display,
        "labels": labels,
        "values": values,
        "unit": (config.get("unit") or "").strip(),
    }
    keys = set(sc.get("keys") or ())
    if "max" in keys or "max_field_slug" in keys:
        out["max"] = _chart_max(db, config)
    if "start_angle" in keys:
        out["start_angle"] = _chart_angle(config, "start_angle", -90)
    if "end_angle" in keys:
        out["end_angle"] = _chart_angle(config, "end_angle", 90)
    return out


# ── display ──────────────────────────────────────────────────────────────────

def _clock_data(db, config, display="digital", source_id=None, fields_snap=None):
    cfg = config if isinstance(config, dict) else {}
    mode, resolved_timezone = _clock_resolved_timezone(db, cfg)
    now_utc = datetime.now(timezone.utc)
    show_seconds = _bool_config(cfg, "show_seconds", default=display != "compact")
    show_date = _bool_config(cfg, "show_date", default=display in ("compact", "world_clock"))
    show_timezone = _bool_config(cfg, "show_timezone", default=True)
    hour_format = _clock_hour_format(cfg)
    layout = _clock_layout(cfg) if display == "world_clock" else "column"
    primary_timezone = resolved_timezone or "UTC"
    primary_label = "Browser time" if mode == "browser" else _clock_label("", primary_timezone, "UTC")
    primary = _clock_face_payload(
        label=primary_label,
        timezone_name=primary_timezone,
        now_utc=now_utc,
        show_seconds=show_seconds,
        show_date=show_date,
        hour_format=hour_format,
    )
    world_rows = []
    if display == "world_clock":
        for row in _clock_world_clocks(cfg):
            try:
                timezone_name = _clock_timezone_name(row["timezone"])
            except ValueError:
                continue
            world_rows.append(_clock_face_payload(
                label=_clock_label(row["label"], timezone_name, "Clock"),
                timezone_name=timezone_name,
                now_utc=now_utc,
                show_seconds=show_seconds,
                show_date=True,
                hour_format=hour_format,
            ))
    return {
        "display": display,
        "timezone_mode": mode,
        "timezone": primary_timezone,
        "server_timezone": primary_timezone,
        "show_seconds": show_seconds,
        "show_date": show_date,
        "show_timezone": show_timezone,
        "hour_format": hour_format,
        "layout": layout,
        "clock": primary,
        "world_clocks": world_rows,
    }


def _render_kv_template(template: str, data) -> str:
    """Substitute ``{{ path }}`` or ``{{ expr }}`` from field data."""
    return render_data_template(template, data if isinstance(data, dict) else {})


def _kv_field_data(field, entry_value=None, transform=None) -> dict:
    """Build template/tone data dict from a Field (+ optional legacy transform)."""
    if field.field_type == "logbook":
        data = entry_value if isinstance(entry_value, dict) else (
            {"value": entry_value} if entry_value is not None else {}
        )
    else:
        data = dict(field.state or {})
    transform = transform if isinstance(transform, list) else []
    if transform and "value" in data:
        computed = apply_ops(data.get("value"), transform)
        if computed is not None:
            data = {**data, "value": computed}
    return data


def _display_data(db, config, display="logbook_list", source_id=None, fields_snap=None):
    from app.models import Field, FieldLogEntry

    snap = fields_snap if isinstance(fields_snap, dict) else {}

    if display == "logbook_list":
        return _display_logbook_list(db, config, source_id=source_id)
    if display == "kv_text":
        template = (config.get("template") or "").strip() or "{{value}}"
        data = dict(snap)
        text = _render_kv_template(template, data)
        return {
            "display": "kv_text",
            "text": text,
            "name": "",
            "template": template,
            "_tone_data": data,
        }

    if display == "toggle":
        field = resolve_field(db, config)
        if field is None:
            return {"display": display, "error": "Choose a field"}
        if field.field_type != "toggle":
            return {"display": display, "error": "This display needs a toggle field", "name": field.name}
        return {
            "display": display, "name": field.name,
            "value": bool((field.state or {}).get("value", False)),
            "field_id": field.id,
        }

    if display == "board":
        return _display_board(db, config, fields_snap=snap)

    if display == "table":
        rows = []
        seen = set()
        for slug in _config_field_slugs(config):
            field = db.query(Field).filter(Field.slug == slug).first()
            if not field or field.id in seen:
                continue
            seen.add(field.id)
            state = field.state or {}
            if field.field_type == "logbook":
                entry = (
                    db.query(FieldLogEntry)
                    .filter(FieldLogEntry.field_id == field.id)
                    .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
                    .first()
                )
                value = entry.value if entry else None
            else:
                value = state if field.field_type == "data" else state.get("value")
            rows.append({
                "field_id": field.id, "name": field.name,
                "field_type": field.field_type, "value": value,
            })
        return {"display": display, "rows": rows}

    return {"display": display, "error": "Unknown display mode"}


def _display_board(db, config, fields_snap=None):
    from app.models import FieldLogEntry

    snap = fields_snap if isinstance(fields_snap, dict) else {}

    cell_kind = _board_cell_kind(config)
    style = resolve_style(config, "board")
    cells = _board_cells(config)
    tone_mode = (config.get("tone") or "").strip().lower()
    if tone_mode not in WIDGET_TONES:
        tone_mode = default_tone("display", "board")

    if not cells:
        return {"display": "board", "error": "Choose at least one field", "items": [], "cell_kind": cell_kind}
    items = []

    for cell in cells:
        transform = cell.get("transform") or []
        unit = (cell.get("unit") or "").strip()
        template = (cell.get("template") or "").strip()

        field = resolve_field(db, cell)

        if cell_kind == "toggle":
            if field is None or field.field_type != "toggle":
                continue
            items.append({
                "kind": "toggle",
                "field_id": field.id,
                "name": field.name,
                "style": style,
                "value": bool((field.state or {}).get("value", False)),
            })
            continue

        name = field.name if field else ""
        bound = {}
        if field is not None:
            entry_val = None
            if field.field_type == "logbook":
                entry = (
                    db.query(FieldLogEntry)
                    .filter(FieldLogEntry.field_id == field.id)
                    .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
                    .first()
                )
                entry_val = entry.value if entry else None
            bound = _kv_field_data(field, entry_val, transform)
        data = _merge_template_data(snap, bound)
        if not template:
            template = f"{{{{value}}}} {unit}".rstrip() if unit else "{{value}}"
        text = _render_kv_template(template, data)
        if not text and data.get("value") is not None:
            text = str(data.get("value"))
        elif not text:
            text = name
        cell_rules = cell.get("tone_rules") if isinstance(cell.get("tone_rules"), list) else []
        row_tone = None
        if tone_mode == "conditional":
            row_tone = resolve_tone_rules(cell_rules, data)
        items.append({
            "kind": "kv_text",
            "field_id": field.id if field else None,
            "name": name,
            "style": style,
            "text": text,
            "tone": row_tone,
        })

    if not items:
        return {"display": "board", "error": "No valid board fields", "items": [], "cell_kind": cell_kind}
    return {"display": "board", "items": items, "cell_kind": cell_kind}


def _display_logbook_list(db, config, source_id=None):
    from app.models import FieldLogEntry

    field, err = _resolve_logbook_list_field(db, config)
    if field is None:
        msg = {
            "choose a field": "Choose a field",
            "that field wasn’t found": "That field wasn’t found",
            "This widget needs a Logbook field": "This widget needs a Logbook field",
            "template must reference a logbook Field": "Template must use a Logbook field",
            "template must reference exactly one logbook Field": (
                "Template must use exactly one Logbook field"
            ),
        }.get(err or "", err or "Choose a field")
        return {"display": "logbook_list", "error": msg, "entries": []}
    template = (config.get("template") or "").strip()
    if "{{" not in template:
        template = ""
    slug = (field.slug or "").strip()
    limit = _int_limit(config, "limit", 20)
    q = (
        db.query(FieldLogEntry)
        .filter(FieldLogEntry.field_id == field.id)
        .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
        .limit(limit)
    )
    if source_id:
        q = q.filter(FieldLogEntry.source_id == source_id)
    rows = q.all()
    entries = []
    for e in rows:
        row = {
            "id": e.id,
            "timestamp": e.timestamp,
            "value": e.value,
            "source_id": e.source_id,
        }
        if template and slug:
            if isinstance(e.value, dict):
                data = {slug: e.value}
            else:
                data = {slug: {"value": e.value}}
            row["text"] = _render_kv_template(template, data)
        entries.append(row)
    tone_data = {}
    if entries and slug:
        latest = entries[0].get("value")
        if isinstance(latest, dict):
            tone_data = {slug: latest}
        else:
            tone_data = {slug: {"value": latest}}
    return {
        "display": "logbook_list",
        "name": field.name,
        "entries": entries,
        "field_id": field.id,
        "template": template,
        "_tone_data": tone_data,
    }


# ── links ────────────────────────────────────────────────────────────────────

def _favicon_for_url(url: str) -> str:
    """DuckDuckGo icon URL for http(s) hosts; else empty."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    host = parsed.hostname
    if not host:
        return ""
    return f"https://icons.duckduckgo.com/ip3/{host}.ico"


def _links_data(db, config, display="list", source_id=None, fields_snap=None):
    items = config.get("items")
    if not isinstance(items, list):
        items = []
    cleaned = []
    for it in items:
        if not isinstance(it, dict):
            continue
        label = (it.get("label") or "").strip()
        url = (it.get("url") or "").strip()
        if not label or not url:
            continue
        cleaned.append({
            "label": label,
            "url": url,
            "favicon": _favicon_for_url(url),
        })
    return {"display": display, "items": cleaned}


def _notes_data(db, config, display="notes", source_id=None, fields_snap=None):
    text = config.get("text") if isinstance(config, dict) else ""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    snap = fields_snap if isinstance(fields_snap, dict) else {}
    return {
        "display": display or "notes",
        "text": text,
        "_tone_data": snap,
    }


# ── system ───────────────────────────────────────────────────────────────────

def _system_data(db, config, display="source_health", source_id=None, fields_snap=None):
    if display == "source_health":
        return {**_source_health_data(db, config, source_id=source_id), "display": display}
    if display == "recent_events":
        return {**_recent_events_data(db, config, source_id=source_id), "display": display}
    if display == "poller_status":
        return {**_poller_status_data(db, config, source_id=source_id), "display": display}
    if display == "metric_summary":
        return {**_metric_summary_data(db, config, source_id=source_id), "display": display}
    return {"display": display, "error": "Unknown display"}


def source_age_status(
    last_seen_at,
    *,
    now: datetime | None = None,
    recent_hours: float = 1.0,
    stale_hours: float = 24.0,
) -> str:
    """Age band for a source: healthy / recent / stale / never / unknown."""
    if last_seen_at is None:
        return "never"
    try:
        ts = last_seen_at
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        age = (now - ts).total_seconds() / 3600
        if age <= recent_hours:
            return "healthy"
        if age <= stale_hours:
            return "recent"
        return "stale"
    except Exception:
        return "unknown"


def _source_health_data(db, config, source_id=None):
    from app.models import Source
    now = datetime.now(timezone.utc)
    threshold_hours = config.get("stale_threshold_hours", 24)
    try:
        threshold_hours = float(threshold_hours)
    except (TypeError, ValueError):
        threshold_hours = 24
    query = db.query(Source)
    if source_id:
        query = query.filter(Source.id == source_id)
    sources = query.order_by(Source.name).all()
    rows = []
    for s in sources:
        if not s.enabled:
            status = "disabled"
        else:
            status = source_age_status(
                s.last_seen_at, now=now, stale_hours=threshold_hours,
            )
        rows.append({
            "name": s.name, "slug": s.slug, "enabled": s.enabled,
            "last_seen": s.last_seen_at, "status": status,
        })
    return {"sources": rows}


def _recent_events_data(db, config, source_id=None):
    from app.models import Event, Source, EventTypeRecord
    limit = _int_limit(config, "limit", 10, hi=50)
    query = db.query(Event).order_by(Event.timestamp.desc()).limit(limit)
    if source_id:
        query = query.filter(Event.source_id == source_id)
    events = query.all()
    rows = []
    for e in events:
        src = db.query(Source).filter(Source.id == e.source_id).first() if e.source_id else None
        et = db.query(EventTypeRecord).filter(EventTypeRecord.id == e.event_type_id).first() if e.event_type_id else None
        rows.append({
            "id": e.id, "source": src.name if src else "unknown",
            "event_type": et.name if et else "unknown",
            "timestamp": e.timestamp, "status": e.status or "pending",
        })
    return {"events": rows}


def _metric_summary_data(db, config, source_id=None):
    from app.models import Field, MetricPoint
    q = db.query(MetricPoint)
    if source_id:
        q = q.filter(MetricPoint.source_id == source_id)
    points = q.count()
    series_q = db.query(sql_func.count(sql_func.distinct(MetricPoint.name)))
    if source_id:
        series_q = series_q.filter(MetricPoint.source_id == source_id)
    series = series_q.scalar() or 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    last_hour = q.filter(MetricPoint.timestamp >= cutoff).count()
    counters = []
    for f in db.query(Field).filter(Field.field_type == "value").order_by(Field.name).all():
        raw = (f.state or {}).get("value", 0)
        try:
            value = float(raw or 0)
        except (TypeError, ValueError):
            value = 0.0
        counters.append({"name": f.name, "value": value})
    return {"series": series, "points": points, "last_hour": last_hour, "counters": counters}


def _poller_status_data(db, config, source_id=None):
    from app.models import PollingSchedule, Source
    query = db.query(PollingSchedule)
    if source_id:
        query = query.filter(PollingSchedule.source_id == source_id)
    schedules = query.order_by(PollingSchedule.name).all()
    rows = []
    for s in schedules:
        src = db.query(Source).filter(Source.id == s.source_id).first() if s.source_id else None
        rows.append({
            "name": s.name, "source": src.name if src else "?",
            "enabled": bool(s.enabled),
            "last_run": s.last_run_at, "next_run": s.next_run_at,
            "success_count": s.success_count or 0,
            "failure_count": s.failure_count or 0,
            "last_error": s.last_error or "",
        })
    return {"schedules": rows}
