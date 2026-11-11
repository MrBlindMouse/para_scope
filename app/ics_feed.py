"""Minimal iCalendar reader for a personal ICS feed.

ponytail: FREQ=DAILY|WEEKLY|MONTHLY, INTERVAL, weekly BYDAY, UNTIL.
No EXDATE, no BYSETPOS, no TZ recurrence tricks. COUNT is ignored when the
event started more than 400 days before the window — upgrade path is a real
iCalendar library if a feed needs exceptions.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_UNESCAPE = re.compile(r"\\([\\nN;,])")
_VEVENT = re.compile(r"BEGIN:VEVENT\r?\n(.*?)END:VEVENT", re.DOTALL)
# Occurrences walked from a jumped cursor. See module note on COUNT.
_COUNT_WALK_DAYS = 400


def unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text or "")


def _unescape(value: str) -> str:
    def repl(m: re.Match) -> str:
        ch = m.group(1)
        if ch in ("n", "N"):
            return "\n"
        return ch

    return _UNESCAPE.sub(repl, value)


def _split_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    bits = head.split(";")
    name = bits[0].strip().upper()
    if not name:
        return None
    params: dict[str, str] = {}
    for bit in bits[1:]:
        if "=" in bit:
            k, v = bit.split("=", 1)
            params[k.strip().upper()] = v.strip()
    return name, params, _unescape(value.strip())


def _parse_dt(value: str, params: dict[str, str], zone: ZoneInfo) -> tuple[datetime, bool] | None:
    raw = value.strip()
    all_day = params.get("VALUE", "").upper() == "DATE" or (len(raw) == 8 and "T" not in raw)
    try:
        if all_day:
            day = datetime.strptime(raw[:8], "%Y%m%d")
            return day.replace(tzinfo=zone), True
        stamp = raw.rstrip("Z")
        if len(stamp) == 13:  # YYYYMMDDTHHMM
            stamp += "00"
        dt = datetime.strptime(stamp[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if raw.endswith("Z"):
        return dt.replace(tzinfo=timezone.utc), False
    tzid = params.get("TZID") or ""
    if tzid:
        try:
            return dt.replace(tzinfo=ZoneInfo(tzid)), False
        except ZoneInfoNotFoundError:
            return dt.replace(tzinfo=zone), False
    return dt.replace(tzinfo=zone), False


def _parse_duration(value: str) -> timedelta | None:
    m = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
        value.strip().upper(),
    )
    if not m or not any(m.groups()):
        return None
    days, hours, minutes, seconds = (int(p or 0) for p in m.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _rrule(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


def _until(value: str, zone: ZoneInfo) -> datetime | None:
    parsed = _parse_dt(value, {}, zone)
    if not parsed:
        return None
    dt, all_day = parsed
    if all_day:
        return dt + timedelta(days=1)
    return dt.astimezone(timezone.utc)


def expand_event(
    props: dict[str, tuple[dict[str, str], str]],
    *,
    window_start: datetime,
    window_end: datetime,
    zone: ZoneInfo,
) -> list[dict]:
    """Return occurrences overlapping [window_start, window_end)."""
    start_raw = props.get("DTSTART")
    if not start_raw:
        return []
    parsed = _parse_dt(start_raw[1], start_raw[0], zone)
    if not parsed:
        return []
    start, all_day = parsed
    end_raw = props.get("DTEND")
    duration_raw = props.get("DURATION")
    if end_raw:
        end_parsed = _parse_dt(end_raw[1], end_raw[0], zone)
        if not end_parsed:
            return []
        end = end_parsed[0]
        if all_day and end <= start:
            end = start + timedelta(days=1)
    elif duration_raw:
        span = _parse_duration(duration_raw[1])
        if span is None or span.total_seconds() <= 0:
            return []
        end = start + span
    else:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    if end <= start:
        return []
    duration = end - start

    uid = (props.get("UID") or ({}, ""))[1].strip()
    title = (props.get("SUMMARY") or ({}, ""))[1].strip() or "(untitled)"
    categories: list[str] = []
    for key, (params, value) in props.items():
        if key == "CATEGORIES" and value:
            categories.extend(p.strip() for p in value.split(",") if p.strip())
    # Multiple CATEGORIES lines collapse in `props` (last wins). Re-read is above
    # only the last line. Callers pass a list via CATEGORIES joined — see parse_ics.

    rule_raw = props.get("RRULE")
    rule = _rrule(rule_raw[1]) if rule_raw else {}
    freq = (rule.get("FREQ") or "").upper()
    if freq and freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        freq = ""
    interval = 1
    if rule.get("INTERVAL"):
        try:
            interval = max(1, int(rule["INTERVAL"]))
        except ValueError:
            interval = 1
    until = _until(rule["UNTIL"], zone) if rule.get("UNTIL") else None
    count: int | None = None
    if rule.get("COUNT"):
        try:
            count = max(0, int(rule["COUNT"]))
        except ValueError:
            count = None
    byday: list[int] = []
    if freq == "WEEKLY" and rule.get("BYDAY"):
        for token in rule["BYDAY"].split(","):
            name = token.strip()[-2:].upper()
            if name in _WEEKDAY:
                byday.append(_WEEKDAY[name])
    if freq == "WEEKLY" and not byday:
        byday = [start.weekday()]

    if not uid:
        uid = hashlib.sha256(f"{title}|{start.isoformat()}".encode()).hexdigest()[:16]

    honor_count = count is not None and (window_start - start) <= timedelta(days=_COUNT_WALK_DAYS)
    produced = 0
    out: list[dict] = []

    def _keep(occ_start: datetime) -> None:
        nonlocal produced
        occ_end = occ_start + duration
        if until and occ_start >= until:
            return
        if honor_count and count is not None and produced >= count:
            return
        produced += 1
        if occ_end <= window_start or occ_start >= window_end:
            return
        if honor_count and count is not None and produced > count:
            return
        utc_start = occ_start.astimezone(timezone.utc)
        out.append({
            "uid": uid[:500],
            "occurrence": utc_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "title": title[:300],
            "starts_at": utc_start,
            "ends_at": occ_end.astimezone(timezone.utc),
            "all_day": all_day,
            "categories": list(categories),
        })

    if not freq:
        _keep(start)
        return out

    # Jump near the window. COUNT on a long-lived event is best-effort (see module note).
    cursor = start
    if freq == "DAILY":
        if start < window_start:
            skip_days = (window_start - start).days
            steps = max(0, (skip_days // interval) - 1)
            cursor = start + timedelta(days=steps * interval)
            if honor_count:
                produced = steps
        guard = 0
        while cursor < window_end and guard < 64:
            guard += 1
            if honor_count and count is not None and produced >= count:
                break
            if until and cursor >= until:
                break
            _keep(cursor)
            cursor = cursor + timedelta(days=interval)
        return out

    if freq == "WEEKLY":
        origin = start - timedelta(days=start.weekday())
        origin = origin.replace(hour=start.hour, minute=start.minute, second=start.second, microsecond=0)
        week_cursor = origin
        if origin < window_start:
            weeks = max(0, (window_start - origin).days // 7)
            steps = max(0, (weeks // interval) - 1)
            week_cursor = origin + timedelta(weeks=steps * interval)
            if honor_count:
                produced = steps * len(byday)
        guard = 0
        while week_cursor < window_end + timedelta(days=7) and guard < 16:
            guard += 1
            for wd in byday:
                day = week_cursor + timedelta(days=wd - week_cursor.weekday())
                occ = day.replace(
                    hour=start.hour, minute=start.minute, second=start.second, microsecond=0,
                )
                if occ < start:
                    continue
                if honor_count and count is not None and produced >= count:
                    return out
                if until and occ >= until:
                    return out
                _keep(occ)
            week_cursor = week_cursor + timedelta(weeks=interval)
        return out

    # MONTHLY — same day-of-month as DTSTART. Missing days (31st) are skipped.
    month_index = start.year * 12 + (start.month - 1)
    if start < window_start:
        months = (window_start.year - start.year) * 12 + (window_start.month - start.month)
        steps = max(0, (months // interval) - 1)
        month_index = start.year * 12 + (start.month - 1) + steps * interval
        if honor_count:
            produced = steps
    guard = 0
    while guard < 18:
        guard += 1
        year, month0 = divmod(month_index, 12)
        month = month0 + 1
        month_index += interval
        if start.day > _month_days(year, month):
            if honor_count:
                produced += 1
            continue
        try:
            occ = start.replace(year=year, month=month, day=start.day)
        except ValueError:
            continue
        if occ < start:
            continue
        if occ >= window_end + timedelta(days=32):
            break
        if honor_count and count is not None and produced >= count:
            break
        if until and occ >= until:
            break
        _keep(occ)
    return out


def _month_days(year: int, month: int) -> int:
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def _event_props(body: str) -> dict[str, tuple[dict[str, str], str]]:
    """Last value wins, except CATEGORIES which are joined."""
    props: dict[str, tuple[dict[str, str], str]] = {}
    cats: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = _split_prop(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "CATEGORIES":
            cats.extend(p.strip() for p in value.split(",") if p.strip())
            continue
        props[name] = (params, value)
    if cats:
        props["CATEGORIES"] = ({}, ",".join(cats))
    return props


def parse_ics(
    text: str,
    *,
    window_start: datetime,
    window_end: datetime,
    zone: ZoneInfo,
) -> list[dict]:
    """Expand VEVENTs to occurrences that overlap the window."""
    unfolded = unfold(text)
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for match in _VEVENT.finditer(unfolded):
        props = _event_props(match.group(1))
        for item in expand_event(
            props, window_start=window_start, window_end=window_end, zone=zone,
        ):
            key = (item["uid"], item["occurrence"])
            if key in seen:
                continue
            seen.add(key)
            found.append(item)
    return found
