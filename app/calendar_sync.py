"""Read an ICS feed into calendar rows and edge events. Does not run rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.ics_feed import parse_ics
from app.kanban import FOCUS_LABELS, FOCUS_SLUGS
from app.models import CalendarItem, PollingSchedule, Source
from app.themes import get_app_settings, normalize_display_timezone

# ponytail: one day back, two weeks forward. A wider window is a poller param later.
WINDOW_BACK = timedelta(days=1)
WINDOW_FORWARD = timedelta(days=14)


def parse_mode_map(text: str) -> dict[str, str]:
    """`Category=deep` lines. Keys are casefolded. Unknown slugs are dropped."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        cat, mode = line.split("=", 1)
        slug = mode.strip().casefold()
        if cat.strip() and slug in FOCUS_SLUGS:
            out[cat.strip().casefold()] = slug
    return out


def match_mode(categories: list[str], mode_map: dict[str, str]) -> str:
    for cat in categories:
        key = cat.strip().casefold()
        if not key:
            continue
        mapped = mode_map.get(key)
        if mapped:
            return mapped
        if key in FOCUS_SLUGS:
            return key
        for slug, label in FOCUS_LABELS.items():
            if key == label.casefold():
                return slug
    return ""


def display_zone(db: Session) -> ZoneInfo:
    name = normalize_display_timezone(get_app_settings(db).display_timezone)
    return ZoneInfo(name)


def apply_feed(
    db: Session,
    source_id: int,
    occurrences: list[dict],
    *,
    now: datetime,
    previous_focus: str,
    mode_map: dict[str, str],
) -> tuple[list[dict], str]:
    """Upsert the window. Return pipeline edges and the focus key to remember.

    Edges are `item.upsert`, `item.removed`, and at most one `focus.changed`.
    Unchanged rows emit nothing.
    """
    existing = {
        (row.uid, row.occurrence): row
        for row in db.query(CalendarItem).filter(CalendarItem.source_id == source_id).all()
    }
    seen: set[tuple[str, str]] = set()
    edges: list[dict] = []
    stamped: list[CalendarItem] = []

    for item in occurrences:
        mode = match_mode(list(item.get("categories") or []), mode_map)
        key = (item["uid"], item["occurrence"])
        seen.add(key)
        row = existing.get(key)
        starts_at = item["starts_at"]
        ends_at = item["ends_at"]
        title = item["title"]
        all_day = bool(item.get("all_day"))
        changed = row is None or any((
            row.title != title,
            _iso(row.starts_at) != _iso(starts_at),
            _iso(row.ends_at) != _iso(ends_at),
            bool(row.all_day) != all_day,
            (row.mode or "") != mode,
        ))
        if row is None:
            row = CalendarItem(
                source_id=source_id,
                uid=item["uid"],
                occurrence=item["occurrence"],
            )
            db.add(row)
        row.title = title
        row.starts_at = starts_at
        row.ends_at = ends_at
        row.all_day = all_day
        row.mode = mode
        stamped.append(row)
        if changed:
            edges.append({"type": "item.upsert", "data": _payload(row)})

    for key, row in existing.items():
        if key in seen:
            continue
        edges.append({
            "type": "item.removed",
            "data": {
                "uid": row.uid,
                "occurrence": row.occurrence,
                "title": row.title,
                "mode": row.mode or "",
            },
        })
        db.delete(row)

    db.flush()
    focus = _pick_focus(stamped, now)
    focus_key = ""
    if focus is not None:
        focus_key = f"{focus.uid}|{focus.occurrence}"
    if focus_key != (previous_focus or ""):
        if focus is None:
            edges.append({
                "type": "focus.changed",
                "data": {"mode": "", "title": "", "starts_at": "", "ends_at": "", "uid": "", "occurrence": ""},
            })
        else:
            edges.append({"type": "focus.changed", "data": _payload(focus)})
    return edges, focus_key


def _payload(row: CalendarItem) -> dict:
    return {
        "uid": row.uid,
        "occurrence": row.occurrence,
        "title": row.title,
        "starts_at": _iso(row.starts_at),
        "ends_at": _iso(row.ends_at),
        "all_day": bool(row.all_day),
        "mode": row.mode or "",
    }


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _pick_focus(rows: list[CalendarItem], now: datetime) -> CalendarItem | None:
    moment = _aware(now)
    active = [row for row in rows if _aware(row.starts_at) <= moment < _aware(row.ends_at)]
    timed = [row for row in active if not row.all_day]
    pool = timed or active
    if not pool:
        return None
    return max(pool, key=lambda row: row.starts_at)


def current_focus(db: Session, now: datetime | None = None) -> CalendarItem | None:
    moment = now or datetime.now(timezone.utc)
    rows = (
        db.query(CalendarItem)
        .filter(CalendarItem.starts_at <= moment, CalendarItem.ends_at > moment)
        .all()
    )
    return _pick_focus(rows, moment)


def week_bounds(anchor, zone: ZoneInfo) -> tuple[datetime, datetime]:
    monday = anchor - timedelta(days=anchor.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=zone)
    return start, start + timedelta(days=7)


def load_week(db: Session, anchor, zone: ZoneInfo) -> list[CalendarItem]:
    start, end = week_bounds(anchor, zone)
    return (
        db.query(CalendarItem)
        .filter(CalendarItem.starts_at < end, CalendarItem.ends_at > start)
        .order_by(CalendarItem.starts_at, CalendarItem.id)
        .all()
    )


def sync_ics(db: Session, schedule: PollingSchedule) -> dict:
    """Fetch the feed, upsert rows, and return edges plus the on_success summary."""
    source = db.query(Source).filter(Source.id == schedule.source_id).first()
    if source is None:
        raise ValueError("Poll source not found")
    url = (schedule.handler_url or "").strip()
    if not url:
        raise ValueError("Calendar URL is required")
    timeout = int(schedule.timeout_seconds or 30)
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ValueError(f"Calendar fetch failed: {exc}") from exc
    if response.status_code >= 400:
        raise ValueError(f"Calendar fetch returned {response.status_code}")
    text = response.text or ""
    now = datetime.now(timezone.utc)
    zone = display_zone(db)
    occurrences = parse_ics(
        text,
        window_start=now - WINDOW_BACK,
        window_end=now + WINDOW_FORWARD,
        zone=zone,
    )
    params = dict(schedule.handler_params or {})
    edges, focus_key = apply_feed(
        db,
        source.id,
        occurrences,
        now=now,
        previous_focus=str(params.get("_focus_key") or ""),
        mode_map=parse_mode_map(str(params.get("mode_map") or "")),
    )
    params["_focus_key"] = focus_key
    schedule.handler_params = params
    return {
        "data": {
            "occurrences": len(occurrences),
            "edges": len(edges),
        },
        "raw": text[:65536],
        "edges": edges,
    }
