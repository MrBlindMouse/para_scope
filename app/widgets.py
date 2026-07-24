"""Dashboard widget registry — kinds with selectable display modes."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

from sqlalchemy import func as sql_func

from app.fields import get_by_path
from app.widget_transforms import apply_ops, extract_number, series_from_points

# ── Kind registry ────────────────────────────────────────────────────────────

KIND_DISPLAYS = {
    "series": ("line", "area", "sparkline"),
    "chart": ("pie", "doughnut", "bar", "stacked_bar"),
    "display": ("logbook_list", "kv_text", "stat", "toggle", "table"),
    "links": ("list", "button_row", "icon_grid"),
    "system": ("source_health", "recent_events", "poller_status", "metric_summary"),
}

KIND_TITLES = {
    "series": "Series Graph",
    "chart": "Chart",
    "display": "Display",
    "links": "Links",
    "system": "System",
}

DISPLAY_TITLES = {
    "line": "Line",
    "area": "Area",
    "sparkline": "Sparkline",
    "pie": "Pie",
    "doughnut": "Doughnut",
    "bar": "Bar",
    "stacked_bar": "Stacked bar",
    "logbook_list": "Logbook list",
    "kv_text": "Key / text",
    "stat": "Stat",
    "toggle": "Toggle",
    "table": "Table",
    "list": "List",
    "button_row": "Button row",
    "icon_grid": "Icon grid",
    "source_health": "Source Health",
    "recent_events": "Recent Events",
    "poller_status": "Poller Status",
    "metric_summary": "Metric Summary",
}

# Visual style variants per display mode (config.style)
DISPLAY_STYLES = {
    "toggle": ("text_color", "led", "badge", "switch"),
    "stat": ("plain", "signed", "compact", "hero"),
    "logbook_list": ("code", "timeline", "cards"),
    "kv_text": ("plain", "mono", "callout"),
    "table": ("plain", "compact", "striped"),
    "line": ("default", "smooth", "stepped", "markers"),
    "area": ("default", "smooth", "stepped", "markers"),
    "sparkline": ("default", "filled"),
    "pie": ("default", "legend_right", "no_legend"),
    "doughnut": ("default", "legend_right", "no_legend"),
    "bar": ("default", "horizontal", "no_legend"),
    "stacked_bar": ("default", "horizontal", "no_legend"),
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
    "signed": "Signed color",
    "compact": "Compact",
    "hero": "Hero",
    "code": "Code list",
    "timeline": "Timeline",
    "cards": "Cards",
    "mono": "Monospace",
    "callout": "Callout",
    "striped": "Striped",
    "default": "Default",
    "smooth": "Smooth",
    "stepped": "Stepped",
    "markers": "Markers",
    "filled": "Filled",
    "legend_right": "Legend right",
    "no_legend": "No legend",
    "horizontal": "Horizontal",
    "emphasized": "Emphasized",
    "table": "Table",
}

# Field binding rules: kind-level, with optional per-display overrides
_BINDING_SERIES = {
    "cardinality": "multi",
    "field_types": ("counter", "logbook"),
    "config_key": "sources",
    "required": True,
}
_BINDING_CHART = {
    "cardinality": "multi",
    "field_types": ("logbook", "counter", "value"),
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
                "config_key": "field_id",
                "required": True,
            },
            "kv_text": {
                "cardinality": "single",
                "field_types": None,  # any
                "config_key": "field_id",
                "required": False,
            },
            "stat": {
                "cardinality": "single",
                "field_types": ("counter", "value"),
                "config_key": "field_id",
                "required": True,
            },
            "toggle": {
                "cardinality": "single",
                "field_types": ("toggle",),
                "config_key": "field_id",
                "required": True,
            },
            "table": {
                "cardinality": "multi",
                "field_types": None,
                "config_key": "field_ids",
                "required": True,
            },
        },
    },
    "links": _BINDING_NONE,
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
            disp_entries.append({
                "id": d,
                "title": DISPLAY_TITLES.get(d, d),
                "styles": styles_for(d),
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
    raw = ((config or {}).get("style") or "").strip()
    allowed = DISPLAY_STYLES.get(display) or ()
    if raw and raw in allowed:
        return raw
    return default_style(display)


def widget_referenced_field_ids(config: dict | None) -> set[int]:
    """All Field ids referenced by a widget config (field_id, field_ids, sources)."""
    cfg = config or {}
    out: set[int] = set()
    fid = _config_field_id(cfg)
    if fid is not None:
        out.add(fid)
    ids = cfg.get("field_ids")
    if isinstance(ids, list):
        for raw in ids:
            try:
                out.add(int(raw))
            except (TypeError, ValueError):
                pass
    elif isinstance(ids, str) and ids.strip():
        for part in ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.add(int(part))
            except ValueError:
                pass
    sources = cfg.get("sources")
    if isinstance(sources, list):
        for src in sources:
            if not isinstance(src, dict):
                continue
            try:
                sid = int(src["field_id"]) if src.get("field_id") not in (None, "") else None
            except (TypeError, ValueError):
                sid = None
            if sid is not None:
                out.add(sid)
    return out


def validate_widget_bindings(db, widgets: list) -> str | None:
    """Return error message if any widget violates Field binding rules."""
    from app.models import Field

    fields_by_id = {f.id: f for f in db.query(Field).all()}
    for i, w in enumerate(widgets):
        if not isinstance(w, dict):
            continue
        kind = (w.get("type") or "").strip()
        disp = (w.get("display") or default_display(kind) or "").strip()
        cfg = w.get("config") if isinstance(w.get("config"), dict) else {}
        label = f"Widget {i + 1} ({KIND_TITLES.get(kind, kind)} / {DISPLAY_TITLES.get(disp, disp)})"
        rule = binding_for(kind, disp)
        card = rule.get("cardinality") or "none"
        style = (cfg.get("style") or "").strip()
        if style and style not in (DISPLAY_STYLES.get(disp) or ()):
            return f"{label}: unknown style '{style}'"
        if card == "none":
            continue
        allowed = rule.get("field_types")  # None = any
        required = bool(rule.get("required"))
        key = rule.get("config_key")

        if key == "sources":
            sources = cfg.get("sources") if isinstance(cfg.get("sources"), list) else []
            # legacy series single field_id
            if not sources and kind == "series" and _config_field_id(cfg) is not None:
                sources = [{"field_id": _config_field_id(cfg)}]
            if not sources and cfg.get("metric_name"):
                continue  # legacy metric_name path
            if required and not sources:
                return f"{label}: at least one Field source is required"
            for src in sources:
                if not isinstance(src, dict):
                    continue
                try:
                    fid = int(src["field_id"]) if src.get("field_id") not in (None, "") else None
                except (TypeError, ValueError):
                    fid = None
                if fid is None:
                    continue
                field = fields_by_id.get(fid)
                if not field:
                    return f"{label}: Field id={fid} not found"
                skind = (src.get("kind") or src.get("field_kind") or "").strip() or field.field_type
                if allowed is not None and field.field_type not in allowed:
                    return f"{label}: Field '{field.name}' type {field.field_type} not allowed"
                if kind == "chart" and skind and allowed is not None and skind not in allowed:
                    return f"{label}: source kind '{skind}' not allowed"
                if kind == "chart" and skind and field.field_type != skind:
                    return f"{label}: Field '{field.name}' is {field.field_type}, not {skind}"
            continue

        if key == "field_ids":
            ids = cfg.get("field_ids")
            if not isinstance(ids, list):
                raw = cfg.get("field_ids") or ""
                if isinstance(raw, str) and raw.strip():
                    ids = [p.strip() for p in raw.split(",") if p.strip()]
                else:
                    ids = []
            parsed = []
            for raw_id in ids:
                try:
                    parsed.append(int(raw_id))
                except (TypeError, ValueError):
                    return f"{label}: invalid field id"
            if required and not parsed:
                return f"{label}: select at least one Field"
            for fid in parsed:
                field = fields_by_id.get(fid)
                if not field:
                    return f"{label}: Field id={fid} not found"
                if allowed is not None and field.field_type not in allowed:
                    return f"{label}: Field '{field.name}' type not allowed"
            continue

        # single field_id
        fid = _config_field_id(cfg)
        if fid is None:
            if required:
                return f"{label}: Field is required"
            continue
        field = fields_by_id.get(fid)
        if not field:
            return f"{label}: Field id={fid} not found"
        if allowed is not None and field.field_type not in allowed:
            return f"{label}: Field '{field.name}' must be one of: {', '.join(allowed)}"
    return None


def fetch_widget_data(widget_type, db, widget_config=None, source_id=None, display=None):
    """Fetch data for a widget kind (+ optional display mode)."""
    config = widget_config or {}
    disp = (display or config.get("display") or default_display(widget_type) or "").strip()
    fn = {
        "series": _series_data,
        "chart": _chart_data,
        "display": _display_data,
        "links": _links_data,
        "system": _system_data,
    }.get(widget_type)
    if not fn:
        return {"error": f"Unknown widget kind: {widget_type}"}
    data = fn(db, config, display=disp, source_id=source_id)
    if isinstance(data, dict):
        data.setdefault("display", disp)
        data["style"] = resolve_style(config, disp)
    return data


def _config_field_id(config) -> int | None:
    raw = config.get("field_id")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _config_transform(config) -> list:
    raw = config.get("transform")
    return raw if isinstance(raw, list) else []


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


# ── series ───────────────────────────────────────────────────────────────────

def _series_source_rows(config) -> list[dict]:
    """Normalize series sources: prefer sources[], else legacy field_id / metric_name."""
    sources = config.get("sources")
    if isinstance(sources, list) and sources:
        return [s for s in sources if isinstance(s, dict) and s.get("field_id") not in (None, "")]
    field_id = _config_field_id(config)
    if field_id is not None:
        return [{
            "field_id": field_id,
            "label": "",
            "value_path": (config.get("value_path") or "").strip(),
            "transform": _config_transform(config),
        }]
    metric_name = (config.get("metric_name") or "").strip()
    if metric_name:
        return [{"metric_name": metric_name, "label": metric_name, "transform": _config_transform(config)}]
    return []


def _series_points_for_source(db, src, *, range_mode, range_hours, range_entries, cutoff, source_id=None):
    """Return (name, points[{ts,v}]) for one series source row."""
    from app.models import Field, FieldLogEntry, MetricPoint

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

    metric_name = (src.get("metric_name") or "").strip()
    if metric_name and src.get("field_id") in (None, ""):
        query = db.query(MetricPoint).filter(MetricPoint.name == metric_name)
        if source_id:
            query = query.filter(MetricPoint.source_id == source_id)
        points = _apply_range(query, MetricPoint.timestamp, MetricPoint.id)
        series = []
        for mp in points:
            v = apply_ops(mp.value, transform) if transform else float(mp.value)
            if v is None:
                continue
            series.append({"ts": mp.timestamp.isoformat(), "v": v})
        return label or metric_name, series, None

    try:
        field_id = int(src["field_id"]) if src.get("field_id") not in (None, "") else None
    except (TypeError, ValueError):
        field_id = None
    if field_id is None:
        return None, [], "field_id required"

    field = db.query(Field).filter(Field.id == field_id).first()
    if not field:
        return None, [], f"Field id={field_id} not found"
    name = label or field.name
    value_path = (src.get("value_path") or "").strip() or None

    if field.field_type == "logbook":
        if not value_path:
            return name, [], "value_path required for logbook series"
        q = db.query(FieldLogEntry).filter(FieldLogEntry.field_id == field_id)
        if source_id:
            q = q.filter(FieldLogEntry.source_id == source_id)
        rows = _apply_range(q, FieldLogEntry.timestamp, FieldLogEntry.id)
        pairs = [(e.timestamp, e.value) for e in rows]
        series = series_from_points(pairs, value_path=value_path, transform=transform)
        return name, series, None

    if field.field_type != "counter":
        return name, [], "series requires a counter or logbook Field"

    query = db.query(MetricPoint).filter(MetricPoint.field_id == field_id)
    if source_id:
        query = query.filter(MetricPoint.source_id == source_id)
    points = _apply_range(query, MetricPoint.timestamp, MetricPoint.id)
    series = []
    for mp in points:
        v = apply_ops(mp.value, transform) if transform else float(mp.value)
        if v is None:
            continue
        series.append({"ts": mp.timestamp.isoformat(), "v": v})
    return name, series, None


def _series_data(db, config, display="line", source_id=None):
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
        return {**base, "error": "field_id or sources required"}

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
        return {**base, "error": errors[0] if errors else "no series data"}

    # Convenience: single-series name at top level (legacy tests / UI)
    name = series_out[0]["name"] if len(series_out) == 1 else ""
    return {**base, "series": series_out, "name": name}


# ── chart (aggregates) ───────────────────────────────────────────────────────

def _chart_source_value(db, src, cutoff, source_id=None):
    """Return (label, numeric_value) for one chart source row."""
    from app.models import Field, FieldLogEntry, MetricPoint

    skind = (src.get("kind") or src.get("field_kind") or "").strip()
    label = (src.get("label") or "").strip()
    try:
        field_id = int(src["field_id"]) if src.get("field_id") not in (None, "") else None
    except (TypeError, ValueError):
        field_id = None
    if field_id is None:
        return None
    field = db.query(Field).filter(Field.id == field_id).first()
    if not field:
        return None
    if not label:
        label = field.name
    if not skind:
        skind = field.field_type
    value_path = (src.get("value_path") or "").strip() or None
    transform = src.get("transform") if isinstance(src.get("transform"), list) else []
    agg = (src.get("agg") or "sum").strip().lower()

    if skind == "logbook" or field.field_type == "logbook":
        q = (
            db.query(FieldLogEntry)
            .filter(FieldLogEntry.field_id == field_id, FieldLogEntry.timestamp >= cutoff)
        )
        if source_id:
            q = q.filter(FieldLogEntry.source_id == source_id)
        entries = q.all()
        if agg == "count":
            if value_path:
                n = sum(1 for e in entries if get_by_path(e.value, value_path) is not None)
            else:
                n = len(entries)
            return label, float(n)
        # sum numeric path values
        if not value_path:
            return label, 0.0
        total = 0.0
        for e in entries:
            v = extract_number(e.value, value_path, transform)
            if v is not None:
                total += v
        return label, total

    if skind in ("counter", "value") or field.field_type in ("counter", "value"):
        if field.field_type == "counter" and agg == "sum_range":
            q = (
                db.query(MetricPoint)
                .filter(MetricPoint.field_id == field_id, MetricPoint.timestamp >= cutoff)
            )
            if source_id:
                q = q.filter(MetricPoint.source_id == source_id)
            total = 0.0
            for mp in q.all():
                v = apply_ops(mp.value, transform) if transform else float(mp.value)
                if v is not None:
                    total += v
            return label, total
        state = field.state or {}
        raw = state.get("value", 0 if field.field_type == "counter" else "")
        if field.field_type == "counter":
            v = apply_ops(raw, transform) if transform else float(raw or 0)
            return label, 0.0 if v is None else v
        # value field: try numeric
        v = apply_ops(raw, transform) if transform else None
        if v is None:
            try:
                v = float(raw)
            except (TypeError, ValueError):
                v = 0.0
        return label, v

    return None


def _chart_data(db, config, display="pie", source_id=None):
    range_hours = _range_hours(config)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=range_hours)
    sources = config.get("sources")
    if not isinstance(sources, list):
        sources = []
    labels, values = [], []
    for src in sources:
        if not isinstance(src, dict):
            continue
        pair = _chart_source_value(db, src, cutoff, source_id=source_id)
        if pair is None:
            continue
        labels.append(pair[0])
        values.append(pair[1])
    return {
        "display": display,
        "labels": labels,
        "values": values,
        "range_hours": range_hours,
        "unit": (config.get("unit") or "").strip(),
    }


# ── display ──────────────────────────────────────────────────────────────────

def _render_kv_template(template: str, data) -> str:
    def repl(m):
        path = m.group(1).strip()
        raw = get_by_path(data, path) if path else data
        return "" if raw is None else str(raw)
    return _TEMPLATE_RE.sub(repl, template or "")


def _display_data(db, config, display="logbook_list", source_id=None):
    from app.models import Field, FieldLogEntry

    if display == "logbook_list":
        return _display_logbook_list(db, config, source_id=source_id)
    if display == "kv_text":
        field_id = _config_field_id(config)
        template = config.get("template") or ""
        data = {}
        name = ""
        if field_id is not None:
            field = db.query(Field).filter(Field.id == field_id).first()
            if not field:
                return {"display": display, "error": f"Field id={field_id} not found"}
            name = field.name
            if field.field_type == "logbook":
                entry = (
                    db.query(FieldLogEntry)
                    .filter(FieldLogEntry.field_id == field_id)
                    .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
                    .first()
                )
                data = entry.value if entry and isinstance(entry.value, dict) else (
                    {"value": entry.value} if entry else {}
                )
            else:
                data = dict(field.state or {})
        text = _render_kv_template(template, data)
        return {"display": display, "text": text, "name": name, "template": template}

    if display == "stat":
        field_id = _config_field_id(config)
        if field_id is None:
            return {"display": display, "error": "field_id required"}
        field = db.query(Field).filter(Field.id == field_id).first()
        if not field:
            return {"display": display, "error": f"Field id={field_id} not found"}
        if field.field_type not in ("counter", "value"):
            return {"display": display, "error": "stat requires counter or value Field", "name": field.name}
        transform = _config_transform(config)
        unit = (config.get("unit") or "").strip()
        raw = (field.state or {}).get("value", 0 if field.field_type == "counter" else "")
        if field.field_type == "counter":
            value = apply_ops(raw, transform) if transform else raw
        else:
            value = apply_ops(raw, transform) if transform else raw
            if value is None and transform:
                value = raw
        sign = ""
        try:
            num = float(value)
            if num > 0:
                sign = "positive"
            elif num < 0:
                sign = "negative"
        except (TypeError, ValueError):
            pass
        return {
            "display": display, "name": field.name, "value": value,
            "field_type": field.field_type, "unit": unit, "field_id": field.id,
            "sign": sign,
        }

    if display == "toggle":
        field_id = _config_field_id(config)
        if field_id is None:
            return {"display": display, "error": "field_id required"}
        field = db.query(Field).filter(Field.id == field_id).first()
        if not field:
            return {"display": display, "error": f"Field id={field_id} not found"}
        if field.field_type != "toggle":
            return {"display": display, "error": "toggle display requires a toggle Field", "name": field.name}
        return {
            "display": display, "name": field.name,
            "value": bool((field.state or {}).get("value", False)),
            "field_id": field.id,
        }

    if display == "table":
        ids = config.get("field_ids")
        if not isinstance(ids, list):
            # allow comma-separated string from forms
            raw = config.get("field_ids") or config.get("field_id")
            if isinstance(raw, str) and raw.strip():
                ids = [p.strip() for p in raw.split(",") if p.strip()]
            elif raw not in (None, ""):
                ids = [raw]
            else:
                ids = []
        rows = []
        for raw_id in ids:
            try:
                fid = int(raw_id)
            except (TypeError, ValueError):
                continue
            field = db.query(Field).filter(Field.id == fid).first()
            if not field:
                continue
            state = field.state or {}
            if field.field_type == "logbook":
                entry = (
                    db.query(FieldLogEntry)
                    .filter(FieldLogEntry.field_id == fid)
                    .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
                    .first()
                )
                value = entry.value if entry else None
            else:
                value = state.get("value")
            rows.append({
                "field_id": field.id, "name": field.name,
                "field_type": field.field_type, "value": value,
            })
        return {"display": display, "rows": rows}

    return {"display": display, "error": f"Unknown display mode: {display}"}


def _display_logbook_list(db, config, source_id=None):
    from app.models import Field, FieldLogEntry

    field_id = _config_field_id(config)
    if field_id is None:
        return {"display": "logbook_list", "error": "field_id required", "entries": [], "series": []}
    field = db.query(Field).filter(Field.id == field_id).first()
    if not field:
        return {"display": "logbook_list", "error": f"Field id={field_id} not found", "entries": [], "series": []}
    if field.field_type != "logbook":
        return {
            "display": "logbook_list", "error": "logbook_list requires a logbook Field",
            "name": field.name, "entries": [], "series": [],
        }
    limit = _int_limit(config, "limit", 20)
    q = (
        db.query(FieldLogEntry)
        .filter(FieldLogEntry.field_id == field_id)
        .order_by(FieldLogEntry.timestamp.desc(), FieldLogEntry.id.desc())
        .limit(limit)
    )
    if source_id:
        q = q.filter(FieldLogEntry.source_id == source_id)
    rows = q.all()
    entries = [
        {"id": e.id, "timestamp": e.timestamp, "value": e.value, "source_id": e.source_id}
        for e in rows
    ]
    value_path = (config.get("value_path") or "").strip() or None
    transform = _config_transform(config)
    unit = (config.get("unit") or "").strip()
    series = []
    if value_path:
        pairs = [(e.timestamp, e.value) for e in reversed(rows)]
        series = series_from_points(pairs, value_path=value_path, transform=transform)
    return {
        "display": "logbook_list",
        "name": field.name,
        "entries": entries,
        "field_id": field.id,
        "series": series,
        "unit": unit,
        "value_path": value_path or "",
    }


# ── links ────────────────────────────────────────────────────────────────────

def _links_data(db, config, display="list", source_id=None):
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
            "icon": (it.get("icon") or "").strip(),
        })
    return {"display": display, "items": cleaned}


# ── system ───────────────────────────────────────────────────────────────────

def _system_data(db, config, display="source_health", source_id=None):
    if display == "source_health":
        return {**_source_health_data(db, config, source_id=source_id), "display": display}
    if display == "recent_events":
        return {**_recent_events_data(db, config, source_id=source_id), "display": display}
    if display == "poller_status":
        return {**_poller_status_data(db, config, source_id=source_id), "display": display}
    if display == "metric_summary":
        return {**_metric_summary_data(db, config, source_id=source_id), "display": display}
    return {"display": display, "error": f"Unknown system display: {display}"}


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
        status = "healthy"
        if not s.enabled:
            status = "disabled"
        elif s.last_seen_at:
            try:
                ts = s.last_seen_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (now - ts).total_seconds() / 3600
                status = "stale" if age > threshold_hours else "healthy"
            except Exception:
                status = "unknown"
        else:
            status = "never"
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
    last_hour = q.filter(MetricPoint.timestamp >= sql_func.now() - timedelta(hours=1)).count()
    counters = []
    for f in db.query(Field).filter(Field.field_type == "counter").order_by(Field.name).all():
        raw = (f.state or {}).get("value", 0)
        try:
            value = float(raw or 0)
        except (TypeError, ValueError):
            value = 0.0
        counters.append({"name": f.name, "value": value})
    return {"series": series, "points": points, "last_hour": last_hour, "counters": counters}


def _poller_status_data(db, config, source_id=None):
    from app.models import PollingSchedule, Source
    query = db.query(PollingSchedule).filter(PollingSchedule.enabled == True)  # noqa: E712
    if source_id:
        query = query.filter(PollingSchedule.source_id == source_id)
    schedules = query.order_by(PollingSchedule.name).all()
    rows = []
    for s in schedules:
        src = db.query(Source).filter(Source.id == s.source_id).first() if s.source_id else None
        rows.append({
            "name": s.name, "source": src.name if src else "?",
            "last_run": s.last_run_at, "next_run": s.next_run_at,
            "success_count": s.success_count or 0,
            "failure_count": s.failure_count or 0,
            "last_error": s.last_error or "",
        })
    return {"schedules": rows}
