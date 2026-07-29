"""Auto-split route module — handlers registered on shared app via include."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
import json

from app.database import get_db
from app.models import (
    DashboardLayout,
    PushSubscription,
    Field,
)
from app.widgets import fetch_widget_data, get_widget_types, validate_widget_bindings
from app.dashboard_layout import (
    GRID_CELL_HEIGHT, GRID_COLUMN_LIVE_MAX, GRID_COLUMN_WIDTH, GRID_COLUMNS,
    GRID_MARGIN, GRID_STACK_BELOW,
    find_widget, grid_stack_column_css, layout_json, merge_geometry,
    normalize_for_save, normalize_widgets, parse_layout_config,
)

from app import webctx as ctx

router = APIRouter()

# route: /api/push/vapid-public-key
@router.get("/api/push/vapid-public-key")
async def push_vapid_public_key():
    from app.webpush_util import vapid_config
    cfg = vapid_config()
    if not cfg:
        return JSONResponse(
            {"error": "Browser notifications aren’t set up on this server"},
            status_code=503,
        )
    return {"public_key": cfg["public_key"]}


# route: /api/push/subscribe
@router.post("/api/push/subscribe")
async def push_subscribe(request: Request, db: Session = Depends(get_db)):
    user = ctx._get_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    endpoint = (body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return JSONResponse({"error": "Couldn’t save notification subscription"}, status_code=400)

    existing = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if existing:
        existing.user_id = user.id
        existing.p256dh = p256dh
        existing.auth = auth
    else:
        db.add(PushSubscription(
            user_id=user.id, endpoint=endpoint, p256dh=p256dh, auth=auth,
        ))
    db.commit()
    return {"ok": True}


# route: /api/push/subscribe
@router.delete("/api/push/subscribe")
async def push_unsubscribe(request: Request, db: Session = Depends(get_db)):
    user = ctx._get_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    endpoint = (body.get("endpoint") or "").strip()
    if not endpoint:
        return JSONResponse({"error": "Missing subscription details"}, status_code=400)
    sub = (
        db.query(PushSubscription)
        .filter(
            PushSubscription.endpoint == endpoint,
            PushSubscription.user_id == user.id,
        )
        .first()
    )
    if sub:
        db.delete(sub)
        db.commit()
    return {"ok": True}


# ── Dashboard (root) ────────────────────────────────────────────────────────

def _get_layout(db: Session) -> DashboardLayout | None:
    """Shared install layout (singleton)."""
    return db.query(DashboardLayout).order_by(DashboardLayout.id).first()


def _load_widgets(db: Session) -> list[dict]:
    """Load shared widgets, migrating ids/geometry and persisting if needed."""
    layout = _get_layout(db)
    if not layout:
        return []
    widgets = parse_layout_config(layout.layout_config)["widgets"]
    widgets, changed = normalize_widgets(widgets)
    if changed:
        layout.layout_config = layout_json(widgets)
        db.commit()
    return widgets


# route: /
@router.get("/")
async def root(request: Request, db: Session = Depends(get_db)):
    user = ctx._get_user(request, db)
    if not user:
        return ctx.templates.TemplateResponse(request, "base.html", {})
    widgets = _load_widgets(db)
    widget_data = {}
    for w in widgets:
        wtype = w.get("type", "")
        wid = w.get("id") or ""
        disp = w.get("display") or ""
        widget_data[wid] = fetch_widget_data(
            wtype, db, widget_config=w.get("config") or {}, display=disp,
        )
    return ctx.templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "widgets": widgets,
            "widget_data": widget_data,
            "grid_columns": GRID_COLUMNS,
            "grid_column_width": GRID_COLUMN_WIDTH,
            "grid_column_live_max": GRID_COLUMN_LIVE_MAX,
            "grid_cell_height": GRID_CELL_HEIGHT,
            "grid_margin": GRID_MARGIN,
            "grid_stack_below": GRID_STACK_BELOW,
            "grid_column_css": grid_stack_column_css(GRID_COLUMN_LIVE_MAX),
        },
    )


# route: /widgets/{widget_type}
@router.get("/widgets/{widget_type}")
async def widget_partial(request: Request, widget_type: str, db: Session = Depends(get_db)):
    """HTMX partial: re-render a single widget's content body."""
    user = ctx._get_user(request, db)
    if not user:
        return HTMLResponse("", status_code=401)

    widget_id = (request.query_params.get("id") or "").strip() or None
    try:
        index = int(request.query_params.get("index") or 0) or None
    except (TypeError, ValueError):
        index = None

    widgets = _load_widgets(db)
    widget = find_widget(widgets, widget_id=widget_id, index=index)
    config = (widget.get("config") or {}) if widget else {}
    display = (widget.get("display") if widget else None) or ""
    canvas_id = widget_id or f"{widget_type}-{index or 0}"

    wdata = fetch_widget_data(
        widget_type, db, widget_config=config, display=display,
    ) or {}
    try:
        from app.themes import appearance_context
        html = ctx.templates.env.get_template(f"widgets/{widget_type}_content.html").render(
            wdata=wdata, request=request, widget_id=canvas_id,
            widget_config=config, display=display or wdata.get("display"),
            **appearance_context(db),
        )
    except Exception:
        return HTMLResponse('<p class="text-muted">Unknown widget</p>')
    return HTMLResponse(html)


# route: /api/dashboard/layout
@router.post("/api/dashboard/layout")
async def api_dashboard_layout(request: Request, db: Session = Depends(get_db)):
    """Merge widget geometry (x/y/w/h) by id into the shared layout."""
    user = ctx._get_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    updates = body.get("widgets") if isinstance(body, dict) else None
    if not isinstance(updates, list):
        return JSONResponse({"error": "Widgets array required"}, status_code=400)

    layout = _get_layout(db)
    if not layout:
        return JSONResponse({"error": "No layout"}, status_code=404)
    widgets = parse_layout_config(layout.layout_config)["widgets"]
    widgets, _ = normalize_widgets(widgets)
    widgets = merge_geometry(widgets, updates)
    layout.layout_config = layout_json(widgets)
    db.commit()
    return {"ok": True}


NOTES_TEXT_MAX = 50_000


# route: /api/dashboard/notes
@router.post("/api/dashboard/notes")
async def api_dashboard_notes(request: Request, db: Session = Depends(get_db)):
    """Persist notes widget body text into shared layout config."""
    user = ctx._get_user(request, db)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    wid = (body.get("id") or "").strip()
    if not wid:
        return JSONResponse({"error": "Widget id required"}, status_code=400)
    text = body.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    if len(text) > NOTES_TEXT_MAX:
        return JSONResponse({"error": "Notes text too long"}, status_code=400)

    layout = _get_layout(db)
    if not layout:
        return JSONResponse({"error": "No layout"}, status_code=404)
    widgets = parse_layout_config(layout.layout_config)["widgets"]
    widgets, _ = normalize_widgets(widgets)
    widget = find_widget(widgets, widget_id=wid)
    if not widget or widget.get("type") != "notes":
        return JSONResponse({"error": "Notes widget not found"}, status_code=404)
    cfg = dict(widget.get("config") or {})
    cfg["text"] = text
    widget["config"] = cfg
    layout.layout_config = layout_json(widgets)
    db.commit()
    return {"ok": True}


# ── Config: Pipeline (Sources → Rules → Actions) ────────────────────────────


# route: /config/dashboard
@router.get("/config/dashboard")
async def config_dashboard(request: Request, db: Session = Depends(get_db)):
    success, error = ctx.get_message_params(request)
    layout_config = _load_widgets(db)

    available_widgets = [
        {
            "type": w["type"],
            "title": w["title"],
            "displays": w["displays"],
            "default_display": w["default_display"],
            "binding": w.get("binding"),
        }
        for w in get_widget_types()
    ]

    return ctx.templates.TemplateResponse(
        request, "config/dashboard.html", {"active": "dashboard",
         "current_widgets": layout_config, "available_widgets": available_widgets,
         "fields": db.query(Field).order_by(Field.name).all(),
         "success": success, "error": error}
    )


# route: /config/dashboard
@router.post("/config/dashboard")
async def save_dashboard(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    widgets_raw = form.get("widgets", "[]").strip()

    try:
        widgets = json.loads(widgets_raw) if widgets_raw else []
    except json.JSONDecodeError:
        return RedirectResponse(
            url=ctx.flash_url("/config/dashboard", error="Couldn’t read the dashboard settings"),
            status_code=303,
        )

    # Preserve geometry from existing layout when the form omits x/y/w/h
    existing = {}
    layout = _get_layout(db)
    if layout:
        for w in parse_layout_config(layout.layout_config)["widgets"]:
            if w.get("id"):
                existing[w["id"]] = w
    for w in widgets:
        if isinstance(w, dict) and w.get("id") and w["id"] in existing:
            prev = existing[w["id"]]
            for key in ("x", "y", "w", "h"):
                if w.get(key) is None and prev.get(key) is not None:
                    w[key] = prev[key]
            # Notes body is edited on the dashboard; config form omits it.
            if w.get("type") == "notes":
                prev_cfg = prev.get("config") if isinstance(prev.get("config"), dict) else {}
                cfg = w.get("config") if isinstance(w.get("config"), dict) else {}
                if "text" not in cfg and "text" in prev_cfg:
                    cfg = dict(cfg)
                    cfg["text"] = prev_cfg["text"]
                    w["config"] = cfg

    widgets = normalize_for_save(widgets)
    err = validate_widget_bindings(db, widgets)
    if err:
        return RedirectResponse(
            url=ctx.flash_url("/config/dashboard", error=err),
            status_code=303,
        )
    payload = layout_json(widgets)

    if layout:
        layout.layout_config = payload
    else:
        layout = DashboardLayout(layout_config=payload)
        db.add(layout)
    db.commit()
    return RedirectResponse(
        url=ctx.flash_url("/config/dashboard", success="Dashboard layout saved"),
        status_code=303,
    )


