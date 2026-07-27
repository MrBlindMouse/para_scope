"""Typed poller registry and built-in handlers."""
from __future__ import annotations

import base64
import hashlib
import imaplib
import json
import logging
import os
import platform
import re
import shutil
import socket
import ssl
import sqlite3
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from sqlalchemy import create_engine, text

from app.database import SQLALCHEMY_DATABASE_URL, SessionLocal
from app.fields import get_by_path
from app.models import Event, EventTypeRecord, PollingSchedule, Secret, Source
from app.pipeline import evaluate_and_dispatch
from app.security import decrypt_secret

logger = logging.getLogger("para_scope.poller")

_POLLERS: dict[str, Callable] = {}
_POLLER_SPECS: dict[str, dict[str, Any]] = {}

POLLER_CATEGORY_LABELS = {
    "url": "URL / HTTP",
    "system": "System",
    "connectivity": "Connectivity / Reachability",
    "storage": "Storage / Filesystem",
    "application": "Application / Domain",
    "external": "External",
}

_HTTP_METHODS = {
    "http_get": "GET",
    "http_post": "POST",
    "http_put": "PUT",
    "http_delete": "DELETE",
}


_OAUTH_TOKEN_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


def _field(
    name: str,
    label: str,
    *,
    param_key: str | None = None,
    input_type: str = "text",
    parse_as: str = "str",
    required: bool = False,
    placeholder: str = "",
    help_text: str = "",
    default: Any = "",
    options: list[tuple[str, str]] | None = None,
    store: str = "params",
    rows: int | None = None,
    secret: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "param_key": param_key or name,
        "input_type": input_type,
        "parse_as": parse_as,
        "required": required,
        "placeholder": placeholder,
        "help_text": help_text,
        "default": default,
        "options": options or [],
        "store": store,
        "rows": rows,
        "secret": secret,
    }


def register_poller(handler_type: str, fn: Callable, *, spec: dict[str, Any] | None = None):
    """Register a poller handler: fn(schedule, db) -> result dict."""
    _POLLERS[handler_type] = fn
    if spec is not None:
        _POLLER_SPECS[handler_type] = {"handler_type": handler_type, **spec}


def get_poller_types() -> list[str]:
    return sorted(_POLLERS.keys())


def get_poller_specs() -> list[dict[str, Any]]:
    return sorted(
        _POLLER_SPECS.values(),
        key=lambda spec: (spec.get("category") or "", spec.get("label") or spec["handler_type"]),
    )


def get_poller_spec(handler_type: str) -> dict[str, Any] | None:
    return _POLLER_SPECS.get(handler_type)


def get_poller_categories() -> list[dict[str, str]]:
    seen = {spec.get("category") for spec in _POLLER_SPECS.values()}
    categories = []
    for slug, label in POLLER_CATEGORY_LABELS.items():
        if slug in seen:
            categories.append({"slug": slug, "label": label})
    return categories


def get_poller_category(handler_type: str) -> str:
    spec = get_poller_spec(handler_type)
    return str(spec.get("category")) if spec else "url"


def parse_poller_form(
    form,
    *,
    existing_params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str], str | None]:
    """Parse subtype-specific config from a form.

    Returns (schedule_values, secret_updates, error_message).
    """
    existing_params = existing_params or {}
    handler_type = (form.get("handler_type") or "http_get").strip()
    spec = get_poller_spec(handler_type)
    if spec is None:
        return None, {}, "Choose a supported poll subtype"

    values = {"handler_type": handler_type, "handler_url": "", "handler_params": {}}
    secret_updates: dict[str, str] = {}

    for field in spec.get("fields", []):
        name = field["name"]
        raw = form.get(name)
        if field["input_type"] == "checkbox":
            raw = form.get(name) in ("1", "on", "true", "True", True)
        elif raw is None:
            raw = ""

        if field.get("secret"):
            raw_secret = str(raw or "").strip()
            clear_requested = form.get(f"{name}_clear") in ("1", "on", "true", "True")
            if raw_secret:
                secret_updates[field["param_key"]] = raw_secret
            elif clear_requested:
                values["handler_params"][field["param_key"]] = None
            elif existing_params.get(field["param_key"]):
                values["handler_params"][field["param_key"]] = existing_params[field["param_key"]]
            continue

        parsed, err = _parse_field_value(field, raw)
        if err:
            return None, {}, err
        if parsed is None and field["store"] == "params":
            continue
        if field["store"] == "url":
            values["handler_url"] = parsed or ""
        else:
            values["handler_params"][field["param_key"]] = parsed

    if spec.get("uses_url") and not values["handler_url"]:
        return None, {}, "URL is required"

    return values, secret_updates, None


def _parse_field_value(field: dict[str, Any], raw: Any) -> tuple[Any, str | None]:
    label = field["label"]
    parse_as = field.get("parse_as", "str")
    required = field.get("required", False)

    if field["input_type"] == "checkbox":
        return bool(raw), None

    text_value = str(raw or "").strip()
    if text_value == "":
        if required:
            return None, f"{label} is required"
        default = field.get("default")
        if parse_as in {"json_dict", "json_any"} and default == "":
            return {}, None
        return (None if default == "" else default), None

    if parse_as == "str":
        return text_value, None
    if parse_as == "int":
        try:
            return int(text_value), None
        except ValueError:
            return None, f"{label} must be a number"
    if parse_as == "float":
        try:
            return float(text_value), None
        except ValueError:
            return None, f"{label} must be a number"
    if parse_as == "json_dict":
        try:
            parsed = json.loads(text_value)
        except json.JSONDecodeError:
            return None, f"{label} must be valid JSON"
        if not isinstance(parsed, dict):
            return None, f"{label} must be a JSON object"
        return parsed, None
    if parse_as == "json_any":
        try:
            return json.loads(text_value), None
        except json.JSONDecodeError:
            return None, f"{label} must be valid JSON"
    if parse_as == "lines":
        return [line.strip() for line in text_value.splitlines() if line.strip()], None
    if parse_as == "csv":
        return [part.strip() for part in text_value.split(",") if part.strip()], None
    if parse_as == "bool":
        return text_value.lower() in ("1", "true", "yes", "on"), None
    return text_value, None


def _build_headers(db, params: dict) -> dict:
    headers = dict(params.get("headers") or {})
    auth_mode = (params.get("auth_mode") or "bearer").strip()
    secret_id = params.get("auth_secret_id")
    if secret_id:
        secret = db.query(Secret).filter(Secret.id == secret_id).first()
        if not secret:
            raise ValueError("Polling secret not found")

        secret_value = decrypt_secret(secret.encrypted_value)
        header_name = params.get("auth_header", "Authorization")

        if auth_mode == "basic":
            # Expect "username:password" stored in the secret.
            if ":" not in secret_value:
                raise ValueError("Basic auth secret must be in the form 'username:password'")
            user, pw = secret_value.split(":", 1)
            b64 = base64.b64encode(f"{user}:{pw}".encode()).decode()
            headers[header_name] = f"Basic {b64}"

        elif auth_mode == "oauth_client_credentials":
            token_url = (params.get("token_url") or "").strip()
            if not token_url:
                raise ValueError("token_url is required for oauth_client_credentials")
            scope = (params.get("scope") or "").strip()
            client_id, client_secret = _parse_id_secret_pair(
                secret_value,
                label="OAuth client secret",
            )
            token = _oauth2_get_access_token(
                token_url=token_url,
                client_id=client_id,
                client_secret=client_secret,
                scope=scope,
            )
            prefix = params.get("auth_prefix", "Bearer ")
            headers[header_name] = f"{prefix}{token}"

        else:
            # Default: bearer-style header injection.
            prefix = params.get("auth_prefix", "Bearer ")
            headers[header_name] = f"{prefix}{secret_value}"
    return headers


def _parse_id_secret_pair(secret_value: str, *, label: str) -> tuple[str, str]:
    if ":" not in secret_value:
        raise ValueError(f"{label} must be in the form 'id:secret'")
    left, right = secret_value.split(":", 1)
    if not left or not right:
        raise ValueError(f"{label} must be in the form 'id:secret'")
    return left, right


def _fetch_oauth2_access_token(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: str,
) -> tuple[str, float]:
    """Fetch an OAuth2 access token (client_credentials)."""
    data: dict[str, str] = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            token_url,
            data=data,
            headers=headers,
            auth=(client_id, client_secret),
        )
        resp.raise_for_status()
        j = resp.json()
    access_token = j["access_token"]
    expires_in = float(j.get("expires_in") or 3600)
    return access_token, expires_in


def _oauth2_get_access_token(*, token_url: str, client_id: str, client_secret: str, scope: str) -> str:
    # ponytail: in-process cache; upgrade path is shared cache across workers.
    key = (token_url, client_id, scope)
    now = time.time()
    cached = _OAUTH_TOKEN_CACHE.get(key)
    if cached and cached.get("expires_at", 0) > now + 15:
        return cached["access_token"]

    access_token, expires_in = _fetch_oauth2_access_token(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
    )
    _OAUTH_TOKEN_CACHE[key] = {
        "access_token": access_token,
        "expires_at": now + expires_in,
    }
    return access_token


def _require_secret(db, secret_id: int | None, *, label: str = "Secret") -> str:
    if not secret_id:
        raise ValueError(f"{label} is required")
    secret = db.query(Secret).filter(Secret.id == secret_id).first()
    if not secret:
        raise ValueError(f"{label} not found")
    return decrypt_secret(secret.encrypted_value)


def _previous_schedule_event(db, source_id: int, schedule_id: int) -> Event | None:
    events = (
        db.query(Event)
        .filter(Event.source_id == source_id)
        .order_by(Event.id.desc())
        .limit(50)
        .all()
    )
    for event in events:
        poll_meta = ((event.normalized_data or {}).get("_poll") or {})
        if poll_meta.get("schedule_id") == schedule_id:
            return event
    return None


def _command_output(command: list[str], *, timeout: int = 30) -> str:
    if not shutil.which(command[0]):
        raise ValueError(f"Required command not found: {command[0]}")
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode != 0:
        raise ValueError(error or output or f"{command[0]} failed")
    return output


def _read_meminfo() -> dict[str, int]:
    meminfo: dict[str, int] = {}
    path = Path("/proc/meminfo")
    if not path.exists():
        return meminfo
    for line in path.read_text().splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        first = rest.strip().split()[0]
        try:
            meminfo[key] = int(first) * 1024
        except (IndexError, ValueError):
            continue
    return meminfo


def _uptime_seconds() -> float | None:
    path = Path("/proc/uptime")
    if not path.exists():
        return None
    try:
        return float(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _http_request_json_or_text(
    *,
    method: str,
    url: str,
    timeout: int | float,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    expected_status: int | None = None,
) -> tuple[httpx.Response, float, Any]:
    t0 = time.perf_counter()
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_body,
        )
    response_time_ms = round((time.perf_counter() - t0) * 1000, 2)
    if expected_status is not None and response.status_code != expected_status:
        raise ValueError(f"Expected HTTP {expected_status}, got {response.status_code}")
    response.raise_for_status()
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = response.text
    return response, response_time_ms, payload


def http_poll(schedule: PollingSchedule, db) -> dict:
    """Execute one HTTP poll for a schedule."""
    params = schedule.handler_params or {}
    method = _HTTP_METHODS.get(schedule.handler_type, "GET")

    if not schedule.handler_url:
        raise ValueError("Poll URL is required")

    headers = _build_headers(db, params)
    timeout = schedule.timeout_seconds or 30
    retries = schedule.retry_count or 0
    last_error = None

    for attempt in range(1 + retries):
        try:
            response, response_time_ms, payload = _http_request_json_or_text(
                method=method,
                url=schedule.handler_url,
                timeout=timeout,
                headers=headers,
                params=params.get("query") or None,
                json_body=params.get("body") if method in ("POST", "PUT") else None,
            )

            extracted = get_by_path(payload, params.get("json_path", ""))
            if extracted is None:
                extracted = payload

            return {
                "ok": True,
                "status_code": response.status_code,
                "data": extracted,
                "raw": response.text[:65536],
                "attempts": attempt + 1,
                "response_time_ms": response_time_ms,
            }
        except Exception as e:
            last_error = e
            if attempt < retries:
                continue

    raise last_error or RuntimeError("poll failed")


def _http_poll_registered(schedule: PollingSchedule, db) -> dict:
    """Thin wrapper so tests can patch `http_poll` and the registry still sees it."""
    return http_poll(schedule, db)


def system_snapshot_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    disk_path = params.get("disk_path") or "/"
    usage = shutil.disk_usage(disk_path)
    meminfo = _read_meminfo()
    load1, load5, load15 = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    data = {
        "status": "ok",
        "hostname": platform.node(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "disk_path": disk_path,
        "disk_total_bytes": usage.total,
        "disk_used_bytes": usage.used,
        "disk_free_bytes": usage.free,
        "load_1m": round(load1, 2),
        "load_5m": round(load5, 2),
        "load_15m": round(load15, 2),
        "mem_total_bytes": meminfo.get("MemTotal"),
        "mem_available_bytes": meminfo.get("MemAvailable"),
        "swap_total_bytes": meminfo.get("SwapTotal"),
        "swap_free_bytes": meminfo.get("SwapFree"),
        "uptime_seconds": _uptime_seconds(),
        "summary": f"{platform.node()} load {round(load1, 2)} free {usage.free}B",
    }
    return {"ok": True, "data": data, "raw": json.dumps(data)[:65536]}


def systemd_failed_units_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    timeout = int(params.get("command_timeout_seconds") or 15)
    output = _command_output(
        ["systemctl", "--failed", "--no-legend", "--plain"],
        timeout=timeout,
    )
    units = []
    for line in output.splitlines():
        parts = line.split()
        if parts:
            units.append(parts[0])
    data = {
        "status": "ok" if not units else "failed_units",
        "failed_units": units,
        "failed_count": len(units),
        "summary": "No failed systemd units" if not units else f"{len(units)} failed units",
    }
    return {"ok": True, "data": data, "raw": output[:65536]}


def journal_recent_errors_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    lines = int(params.get("line_count") or 20)
    timeout = int(params.get("command_timeout_seconds") or 20)
    output = _command_output(
        ["journalctl", "-p", "err", "-n", str(lines), "--no-pager", "-o", "short-iso"],
        timeout=timeout,
    )
    entries = [line for line in output.splitlines() if line.strip()]
    data = {
        "status": "ok" if not entries else "errors_present",
        "entry_count": len(entries),
        "entries": entries,
        "summary": "No recent journal errors" if not entries else f"{len(entries)} recent error entries",
    }
    return {"ok": True, "data": data, "raw": output[:65536]}


def tcp_connect_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    host = (params.get("host") or "").strip()
    port = int(params.get("port") or 0)
    if not host or port < 1:
        raise ValueError("Host and port are required")
    timeout = float(schedule.timeout_seconds or 10)
    t0 = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout):
        pass
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    data = {
        "status": "ok",
        "host": host,
        "port": port,
        "latency_ms": latency_ms,
        "summary": f"TCP {host}:{port} reachable",
    }
    return {"ok": True, "data": data, "raw": json.dumps(data), "response_time_ms": latency_ms}


def icmp_ping_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    host = (params.get("host") or "").strip()
    if not host:
        raise ValueError("Host is required")
    count = int(params.get("count") or 1)
    timeout = int(schedule.timeout_seconds or 10)
    output = _command_output(
        ["ping", "-c", str(max(1, count)), "-W", str(max(1, timeout)), host],
        timeout=timeout + 2,
    )
    m = re.search(r"=\s*[\d.]+/([\d.]+)/", output)
    latency_ms = float(m.group(1)) if m else None
    data = {
        "status": "ok",
        "host": host,
        "count": count,
        "latency_ms": latency_ms,
        "summary": f"Ping {host} ok",
    }
    return {
        "ok": True,
        "data": data,
        "raw": output[:65536],
        "response_time_ms": latency_ms,
    }


def dns_resolve_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    host = (params.get("host") or "").strip()
    family = (params.get("family") or "any").strip()
    if not host:
        raise ValueError("Host is required")
    family_map = {
        "any": socket.AF_UNSPEC,
        "ipv4": socket.AF_INET,
        "ipv6": socket.AF_INET6,
    }
    infos = socket.getaddrinfo(host, None, family=family_map.get(family, socket.AF_UNSPEC))
    addresses = sorted({item[4][0] for item in infos})
    data = {
        "status": "ok",
        "host": host,
        "family": family,
        "addresses": addresses,
        "address_count": len(addresses),
        "summary": f"Resolved {len(addresses)} address(es) for {host}",
    }
    return {"ok": True, "data": data, "raw": json.dumps(data)}


def cert_expiry_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    host = (params.get("host") or "").strip()
    port = int(params.get("port") or 443)
    if not host:
        raise ValueError("Host is required")
    timeout = float(schedule.timeout_seconds or 10)
    ctx = ssl.create_default_context()
    t0 = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
            cert = tls_sock.getpeercert()
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    not_after = cert.get("notAfter")
    if not not_after:
        raise ValueError("Certificate expiry not available")
    expires_at = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    remaining = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    data = {
        "status": "ok",
        "host": host,
        "port": port,
        "expires_at": expires_at.isoformat(),
        "expires_in_seconds": remaining,
        "latency_ms": latency_ms,
        "summary": f"TLS certificate for {host} expires {expires_at.date().isoformat()}",
    }
    return {"ok": True, "data": data, "raw": json.dumps(data), "response_time_ms": latency_ms}


def disk_free_space_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    path = (params.get("path") or "/").strip() or "/"
    usage = shutil.disk_usage(path)
    data = {
        "status": "ok",
        "path": path,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": round((usage.free / usage.total) * 100, 2) if usage.total else 0,
        "summary": f"{path} free {usage.free}B",
    }
    return {"ok": True, "data": data, "raw": json.dumps(data)}


def backup_age_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    path = Path((params.get("path") or "").strip())
    if not path:
        raise ValueError("Backup path is required")
    if not path.exists():
        raise ValueError("Backup path does not exist")
    stat = path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    age_seconds = int((datetime.now(timezone.utc) - mtime).total_seconds())
    data = {
        "status": "ok",
        "path": str(path),
        "modified_at": mtime.isoformat(),
        "age_seconds": age_seconds,
        "summary": f"{path.name} age {age_seconds}s",
    }
    return {"ok": True, "data": data, "raw": json.dumps(data)}


def git_status_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    repo_path = (params.get("repo_path") or ".").strip() or "."
    timeout = int(params.get("command_timeout_seconds") or 15)
    output = _command_output(
        ["git", "-C", repo_path, "status", "--short", "--branch"],
        timeout=timeout,
    )
    lines = [line for line in output.splitlines() if line.strip()]
    dirty = any(not line.startswith("##") for line in lines)
    data = {
        "status": "dirty" if dirty else "clean",
        "repo_path": repo_path,
        "dirty": dirty,
        "line_count": len(lines),
        "lines": lines,
        "summary": f"{repo_path} is {'dirty' if dirty else 'clean'}",
    }
    return {"ok": True, "data": data, "raw": output[:65536]}


def rss_atom_change_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    url = schedule.handler_url or ""
    if not url:
        raise ValueError("URL is required")
    response, response_time_ms, payload = _http_request_json_or_text(
        method="GET",
        url=url,
        timeout=schedule.timeout_seconds or 30,
    )
    text_payload = payload if isinstance(payload, str) else response.text
    root = ET.fromstring(text_payload)
    items: list[dict[str, str]] = []
    if root.tag.endswith("rss") or root.tag == "rss":
        entries = root.findall("./channel/item")
        for entry in entries[:5]:
            items.append({
                "id": (entry.findtext("guid") or entry.findtext("link") or entry.findtext("title") or "").strip(),
                "title": (entry.findtext("title") or "").strip(),
                "updated": (entry.findtext("pubDate") or "").strip(),
            })
    else:
        ns_entry = "{http://www.w3.org/2005/Atom}entry"
        for entry in root.findall(ns_entry)[:5]:
            items.append({
                "id": (entry.findtext("{http://www.w3.org/2005/Atom}id") or "").strip(),
                "title": (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip(),
                "updated": (
                    entry.findtext("{http://www.w3.org/2005/Atom}updated")
                    or entry.findtext("{http://www.w3.org/2005/Atom}published")
                    or ""
                ).strip(),
            })
    fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
    previous = _previous_schedule_event(db, schedule.source_id, schedule.id)
    previous_fp = ((previous.normalized_data or {}).get("feed") or {}).get("fingerprint") if previous else None
    changed = previous_fp is not None and previous_fp != fingerprint
    latest = items[0] if items else {}
    data = {
        "status": "changed" if changed else "ok",
        "feed": {
            "changed": changed,
            "fingerprint": fingerprint,
            "item_count": len(items),
            "latest_id": latest.get("id"),
            "latest_title": latest.get("title"),
            "latest_updated": latest.get("updated"),
            "items": items,
        },
        "summary": f"Feed {'changed' if changed else 'checked'} ({len(items)} items)",
    }
    return {
        "ok": True,
        "status_code": response.status_code,
        "data": data,
        "raw": response.text[:65536],
        "response_time_ms": response_time_ms,
    }


def public_http_status_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    url = schedule.handler_url or ""
    if not url:
        raise ValueError("URL is required")
    method = (params.get("method") or "GET").upper()
    expected_status = params.get("expected_status")
    expected_status = int(expected_status) if expected_status not in (None, "") else None
    response, response_time_ms, payload = _http_request_json_or_text(
        method=method,
        url=url,
        timeout=schedule.timeout_seconds or 30,
        expected_status=expected_status,
    )
    body = payload if isinstance(payload, str) else json.dumps(payload)
    data = {
        "status": "ok",
        "url": url,
        "method": method,
        "status_code": response.status_code,
        "latency_ms": response_time_ms,
        "summary": f"{method} {url} -> {response.status_code}",
    }
    return {
        "ok": True,
        "status_code": response.status_code,
        "data": data,
        "raw": body[:65536],
        "response_time_ms": response_time_ms,
    }


def database_health_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    database_url = (params.get("database_url") or SQLALCHEMY_DATABASE_URL).strip()
    t0 = time.perf_counter()
    if database_url.startswith("sqlite:///"):
        sqlite_path = database_url.removeprefix("sqlite:///")
        conn = sqlite3.connect(sqlite_path)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    else:
        engine = create_engine(database_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    data = {
        "status": "ok",
        "database_url": database_url,
        "latency_ms": latency_ms,
        "summary": f"Database ok in {latency_ms}ms",
    }
    return {"ok": True, "data": data, "raw": json.dumps(data), "response_time_ms": latency_ms}


def log_pattern_watch_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    file_path = Path((params.get("file_path") or "").strip())
    patterns = list(params.get("patterns") or [])
    read_bytes = int(params.get("read_bytes") or 65536)
    if not file_path:
        raise ValueError("Log file path is required")
    if not file_path.exists():
        raise ValueError("Log file not found")
    raw = file_path.read_bytes()[-read_bytes:].decode("utf-8", errors="replace")
    matches: dict[str, int] = {}
    for pattern in patterns:
        try:
            matches[pattern] = len(re.findall(pattern, raw, flags=re.MULTILINE))
        except re.error as e:
            raise ValueError(f"Invalid regex: {pattern}") from e
    matched = {pattern: count for pattern, count in matches.items() if count}
    data = {
        "status": "matches" if matched else "ok",
        "file_path": str(file_path),
        "patterns": patterns,
        "matches": matched,
        "summary": "Log pattern match found" if matched else "No log pattern matches",
    }
    return {"ok": True, "data": data, "raw": raw[:65536]}


def home_assistant_snapshot_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    base_url = (params.get("base_url") or "").rstrip("/")
    resource_path = (params.get("resource_path") or "/api/states").strip() or "/api/states"
    if not base_url:
        raise ValueError("Base URL is required")
    headers = {}
    token_secret_id = params.get("auth_secret_id")
    if token_secret_id:
        headers["Authorization"] = f"Bearer {_require_secret(db, token_secret_id, label='Home Assistant token')}"
    response, response_time_ms, payload = _http_request_json_or_text(
        method="GET",
        url=f"{base_url}{resource_path}",
        timeout=schedule.timeout_seconds or 30,
        headers=headers,
    )
    states = payload if isinstance(payload, list) else []
    domains: dict[str, int] = {}
    for item in states:
        entity_id = str(item.get("entity_id") or "")
        domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
        domains[domain] = domains.get(domain, 0) + 1
    data = {
        "status": "ok",
        "base_url": base_url,
        "resource_path": resource_path,
        "entity_count": len(states),
        "domains": domains,
        "summary": f"Home Assistant returned {len(states)} entities",
    }
    return {
        "ok": True,
        "status_code": response.status_code,
        "data": data,
        "raw": response.text[:65536],
        "response_time_ms": response_time_ms,
    }


def imap_unread_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    host = (params.get("host") or "").strip()
    username = (params.get("username") or "").strip()
    mailbox = (params.get("mailbox") or "INBOX").strip() or "INBOX"
    port = int(params.get("port") or 993)
    use_ssl = params.get("use_ssl", True) is not False
    password = _require_secret(db, params.get("password_secret_id"), label="IMAP password")
    if not host or not username:
        raise ValueError("Host and username are required")

    client = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    try:
        client.login(username, password)
        status, _ = client.select(mailbox, readonly=True)
        if status != "OK":
            raise ValueError("Could not open mailbox")
        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            raise ValueError("Could not search mailbox")
    finally:
        try:
            client.logout()
        except Exception:
            pass

    uids = [uid for uid in (data[0].decode().split() if data and data[0] else []) if uid]
    unread_count = len(uids)
    max_uid = int(uids[-1]) if uids else 0
    previous = _previous_schedule_event(db, schedule.source_id, schedule.id)
    previous_max = int(((previous.normalized_data or {}).get("imap") or {}).get("max_uid") or 0) if previous else 0
    new_since_last = unread_count if previous is None else len([uid for uid in uids if int(uid) > previous_max])
    data = {
        "status": "ok",
        "host": host,
        "mailbox": mailbox,
        "unread_count": unread_count,
        "new_since_last": new_since_last,
        "imap": {"max_uid": max_uid},
        "summary": f"{unread_count} unread in {mailbox}",
    }
    return {"ok": True, "data": data, "raw": json.dumps(data)}


def _parse_whois_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    candidates = [value]
    if value.endswith("Z"):
        candidates.append(value[:-1] + "+00:00")
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%Y.%m.%d",
        "%d.%m.%Y %H:%M:%S",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def domain_expiry_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    domain = (params.get("domain") or "").strip()
    if not domain:
        raise ValueError("Domain is required")
    timeout = int(params.get("command_timeout_seconds") or 20)
    output = _command_output(["whois", domain], timeout=timeout)
    match = re.search(
        r"(?im)^(Registry Expiry Date|Registrar Registration Expiration Date|Expiry Date|Expiration Date):\s*(.+)$",
        output,
    )
    if not match:
        raise ValueError("Could not find domain expiry in whois output")
    expires_at = _parse_whois_datetime(match.group(2))
    if expires_at is None:
        raise ValueError("Could not parse domain expiry date")
    seconds_left = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    data = {
        "status": "ok",
        "domain": domain,
        "expires_at": expires_at.isoformat(),
        "expires_in_seconds": seconds_left,
        "summary": f"{domain} expires {expires_at.date().isoformat()}",
    }
    return {"ok": True, "data": data, "raw": output[:65536]}


def local_llm_http_status_poll(schedule: PollingSchedule, db) -> dict:
    params = schedule.handler_params or {}
    url = schedule.handler_url or "http://127.0.0.1:11434/api/tags"
    response, response_time_ms, payload = _http_request_json_or_text(
        method="GET",
        url=url,
        timeout=schedule.timeout_seconds or 15,
    )
    model_count = None
    if isinstance(payload, dict):
        models = payload.get("models")
        if isinstance(models, list):
            model_count = len(models)
    data = {
        "status": "ok",
        "url": url,
        "model_count": model_count,
        "latency_ms": response_time_ms,
        "summary": f"Local LLM endpoint ok ({model_count if model_count is not None else 'unknown'} models)",
    }
    return {
        "ok": True,
        "status_code": response.status_code,
        "data": data,
        "raw": response.text[:65536],
        "response_time_ms": response_time_ms,
    }


_COMMON_HTTP_FIELDS = [
    _field(
        "handler_url",
        "URL",
        input_type="url",
        required=True,
        placeholder="https://api.example.com/data",
        store="url",
        help_text="Full URL Para-Scope checks on each run.",
    ),
    _field(
        "event_type",
        "Success event name",
        placeholder="on_success",
        help_text="Optional custom event type for successful runs.",
    ),
    _field(
        "json_path",
        "JSON path",
        placeholder="payload.items.0",
        help_text="Optional dotted path inside the JSON response.",
    ),
    _field(
        "headers",
        "Headers (JSON)",
        input_type="textarea",
        parse_as="json_dict",
        default={},
        rows=3,
        help_text="Optional request headers as a JSON object.",
    ),
    _field(
        "query",
        "Query params (JSON)",
        input_type="textarea",
        parse_as="json_dict",
        default={},
        rows=3,
        help_text="Optional query string parameters as a JSON object.",
    ),
    _field(
        "body",
        "Body (JSON)",
        input_type="textarea",
        parse_as="json_any",
        default={},
        rows=4,
        help_text="JSON body for POST and PUT requests.",
    ),
    _field(
        "auth_mode",
        "Auth mode",
        input_type="select",
        parse_as="str",
        default="bearer",
        options=[
            ("bearer", "Bearer token header"),
            ("basic", "HTTP Basic auth"),
            ("oauth_client_credentials", "OAuth2 client credentials"),
        ],
        help_text="How to convert the stored secret into an Authorization header.",
    ),
    _field(
        "auth_header",
        "Auth header",
        default="Authorization",
        help_text="Header name used for Authorization-style auth.",
    ),
    _field(
        "auth_prefix",
        "Auth prefix",
        default="Bearer ",
        help_text="Prefix placed before the token for bearer/oauth. (Basic ignores this.)",
    ),
    _field(
        "auth_secret_value",
        "Poll secret",
        param_key="auth_secret_id",
        input_type="password",
        secret=True,
        help_text=(
            "Optional encrypted secret. For bearer: token. For basic/oauth: store 'id:secret' or 'username:password'."
        ),
    ),
    _field(
        "token_url",
        "OAuth token URL",
        input_type="url",
        placeholder="https://auth.example.com/oauth2/token",
        help_text="Required when Auth mode is OAuth2 client credentials.",
        default="",
    ),
    _field(
        "scope",
        "OAuth scope (optional)",
        placeholder="read write",
        help_text="Optional OAuth2 scope for oauth_client_credentials.",
        default="",
    ),
]

for _ht, _method in _HTTP_METHODS.items():
    register_poller(
        _ht,
        _http_poll_registered,
        spec={
            "label": _method,
            "category": "url",
            "summary": f"{_method} request on a schedule",
            "uses_url": True,
            "fields": _COMMON_HTTP_FIELDS,
        },
    )

register_poller(
    "system_snapshot",
    system_snapshot_poll,
    spec={
        "label": "Host snapshot",
        "category": "system",
        "summary": "Load, memory, disk, and uptime snapshot",
        "uses_url": False,
        "fields": [
            _field(
                "disk_path",
                "Disk path",
                default="/",
                placeholder="/",
                help_text="Disk path to summarize in the snapshot.",
            ),
        ],
    },
)
register_poller(
    "systemd_failed_units",
    systemd_failed_units_poll,
    spec={
        "label": "Failed systemd units",
        "category": "system",
        "summary": "Snapshot of failed systemd units",
        "uses_url": False,
        "fields": [
            _field(
                "command_timeout_seconds",
                "Command timeout (s)",
                parse_as="int",
                input_type="number",
                default=15,
            ),
        ],
    },
)
register_poller(
    "journal_recent_errors",
    journal_recent_errors_poll,
    spec={
        "label": "Recent journal errors",
        "category": "system",
        "summary": "Recent error-level journal entries",
        "uses_url": False,
        "fields": [
            _field("line_count", "Line count", parse_as="int", input_type="number", default=20),
            _field(
                "command_timeout_seconds",
                "Command timeout (s)",
                parse_as="int",
                input_type="number",
                default=20,
            ),
        ],
    },
)
register_poller(
    "tcp_connect",
    tcp_connect_poll,
    spec={
        "label": "TCP connect",
        "category": "connectivity",
        "summary": "Check whether a TCP port is reachable",
        "uses_url": False,
        "fields": [
            _field("host", "Host", required=True, placeholder="example.com"),
            _field("port", "Port", required=True, parse_as="int", input_type="number", placeholder="443"),
        ],
    },
)
register_poller(
    "icmp_ping",
    icmp_ping_poll,
    spec={
        "label": "ICMP ping",
        "category": "connectivity",
        "summary": "Ping a host",
        "uses_url": False,
        "fields": [
            _field("host", "Host", required=True, placeholder="192.168.1.1"),
            _field("count", "Ping count", parse_as="int", input_type="number", default=1),
        ],
    },
)
register_poller(
    "dns_resolve",
    dns_resolve_poll,
    spec={
        "label": "DNS resolve",
        "category": "connectivity",
        "summary": "Resolve A and AAAA records via getaddrinfo",
        "uses_url": False,
        "fields": [
            _field("host", "Host", required=True, placeholder="example.com"),
            _field(
                "family",
                "Address family",
                input_type="select",
                default="any",
                options=[("any", "Any"), ("ipv4", "IPv4 only"), ("ipv6", "IPv6 only")],
            ),
        ],
    },
)
register_poller(
    "cert_expiry",
    cert_expiry_poll,
    spec={
        "label": "TLS cert expiry",
        "category": "connectivity",
        "summary": "Read a TLS certificate and calculate time to expiry",
        "uses_url": False,
        "fields": [
            _field("host", "Host", required=True, placeholder="example.com"),
            _field("port", "Port", parse_as="int", input_type="number", default=443),
        ],
    },
)
register_poller(
    "disk_free_space",
    disk_free_space_poll,
    spec={
        "label": "Path free space",
        "category": "storage",
        "summary": "Summarize total, used, and free bytes for a path",
        "uses_url": False,
        "fields": [
            _field("path", "Path", required=True, placeholder="/var/lib", default="/"),
        ],
    },
)
register_poller(
    "backup_age",
    backup_age_poll,
    spec={
        "label": "Backup age",
        "category": "storage",
        "summary": "Age since a backup file or directory was updated",
        "uses_url": False,
        "fields": [
            _field("path", "Backup path", required=True, placeholder="/backups/latest.tar.zst"),
        ],
    },
)
register_poller(
    "git_status",
    git_status_poll,
    spec={
        "label": "Git status",
        "category": "application",
        "summary": "Run git status for a repository",
        "uses_url": False,
        "fields": [
            _field("repo_path", "Repository path", required=True, placeholder="/srv/app"),
            _field(
                "command_timeout_seconds",
                "Command timeout (s)",
                parse_as="int",
                input_type="number",
                default=15,
            ),
        ],
    },
)
register_poller(
    "rss_atom_change",
    rss_atom_change_poll,
    spec={
        "label": "RSS / Atom change",
        "category": "external",
        "summary": "Fetch an RSS or Atom feed and detect top-item changes",
        "uses_url": True,
        "fields": [
            _field(
                "handler_url",
                "Feed URL",
                input_type="url",
                required=True,
                placeholder="https://example.com/feed.xml",
                store="url",
            ),
        ],
    },
)
register_poller(
    "public_http_status",
    public_http_status_poll,
    spec={
        "label": "Public endpoint status",
        "category": "external",
        "summary": "Check public endpoint status and latency",
        "uses_url": True,
        "fields": [
            _field(
                "handler_url",
                "URL",
                input_type="url",
                required=True,
                placeholder="https://status.example.com/health",
                store="url",
            ),
            _field(
                "method",
                "Method",
                input_type="select",
                default="GET",
                options=[("GET", "GET"), ("HEAD", "HEAD")],
            ),
            _field(
                "expected_status",
                "Expected status",
                input_type="number",
                parse_as="int",
                placeholder="200",
            ),
        ],
    },
)
register_poller(
    "database_health",
    database_health_poll,
    spec={
        "label": "Database health",
        "category": "application",
        "summary": "Run SELECT 1 against the app DB or a configured DSN",
        "uses_url": False,
        "fields": [
            _field(
                "database_url",
                "Database URL",
                placeholder="Leave blank to use Para-Scope's DB",
                help_text="Optional SQLAlchemy database URL. Blank uses the current app DB.",
            ),
        ],
    },
)
register_poller(
    "log_pattern_watch",
    log_pattern_watch_poll,
    spec={
        "label": "Log pattern watch",
        "category": "application",
        "summary": "Scan the tail of a log file for regex matches",
        "uses_url": False,
        "fields": [
            _field("file_path", "Log file", required=True, placeholder="/var/log/app.log"),
            _field(
                "patterns",
                "Patterns",
                input_type="textarea",
                parse_as="lines",
                rows=4,
                required=True,
                help_text="One regex per line.",
            ),
            _field(
                "read_bytes",
                "Read bytes",
                parse_as="int",
                input_type="number",
                default=65536,
            ),
        ],
    },
)
register_poller(
    "home_assistant_snapshot",
    home_assistant_snapshot_poll,
    spec={
        "label": "Home Assistant snapshot",
        "category": "application",
        "summary": "Fetch states from Home Assistant",
        "uses_url": False,
        "fields": [
            _field("base_url", "Base URL", required=True, placeholder="http://homeassistant.local:8123"),
            _field("resource_path", "API path", default="/api/states", placeholder="/api/states"),
            _field(
                "auth_secret_value",
                "Access token",
                param_key="auth_secret_id",
                input_type="password",
                secret=True,
                help_text="Long-lived access token stored encrypted.",
            ),
        ],
    },
)
register_poller(
    "imap_unread",
    imap_unread_poll,
    spec={
        "label": "IMAP unread",
        "category": "external",
        "summary": "Count unread messages in a mailbox",
        "uses_url": False,
        "fields": [
            _field("host", "Host", required=True, placeholder="imap.example.com"),
            _field("port", "Port", parse_as="int", input_type="number", default=993),
            _field("username", "Username", required=True, placeholder="alerts@example.com"),
            _field("mailbox", "Mailbox", default="INBOX"),
            _field(
                "use_ssl",
                "Use SSL",
                input_type="checkbox",
                parse_as="bool",
                default=True,
            ),
            _field(
                "password_secret_value",
                "Password",
                param_key="password_secret_id",
                input_type="password",
                secret=True,
                help_text="IMAP password stored encrypted.",
            ),
        ],
    },
)
register_poller(
    "domain_expiry",
    domain_expiry_poll,
    spec={
        "label": "Domain expiry",
        "category": "external",
        "summary": "Read domain expiry from whois",
        "uses_url": False,
        "fields": [
            _field("domain", "Domain", required=True, placeholder="example.com"),
            _field(
                "command_timeout_seconds",
                "Command timeout (s)",
                parse_as="int",
                input_type="number",
                default=20,
            ),
        ],
    },
)
register_poller(
    "local_llm_http_status",
    local_llm_http_status_poll,
    spec={
        "label": "Local LLM status",
        "category": "application",
        "summary": "Check a local LLM HTTP endpoint such as Ollama",
        "uses_url": True,
        "fields": [
            _field(
                "handler_url",
                "Endpoint URL",
                input_type="url",
                required=True,
                placeholder="http://127.0.0.1:11434/api/tags",
                store="url",
            ),
        ],
    },
)


def run_schedule(schedule_id: int) -> bool:
    """Entry point called by the scheduler for a single schedule id.

    Returns True on success, False on failure (or no-op skip).
    """
    db = SessionLocal()
    success = False
    try:
        schedule = db.query(PollingSchedule).filter(PollingSchedule.id == schedule_id).first()
        if not schedule or not schedule.enabled:
            return False

        source = db.query(Source).filter(Source.id == schedule.source_id).first()
        if not source or not source.enabled:
            return False

        now = datetime.now(timezone.utc)
        handler = _POLLERS.get(schedule.handler_type)
        t0 = time.perf_counter()
        try:
            if handler is None:
                raise ValueError("Unknown poll method")
            result = handler(schedule, db)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            if isinstance(result, dict) and result.get("response_time_ms") is None:
                result["response_time_ms"] = elapsed_ms
            _create_poll_event(db, schedule, source, result, outcome="on_success")
            _create_poll_event(
                db, schedule, source, result,
                outcome="on_success", type_name="always",
            )
            schedule.success_count = (schedule.success_count or 0) + 1
            schedule.last_error = ""
            success = True
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            schedule.failure_count = (schedule.failure_count or 0) + 1
            schedule.last_error = str(e)[:2000]
            logger.exception(
                "Poll failed schedule_id=%s name=%s",
                schedule.id, schedule.name,
            )
            fail_result = {
                "ok": False,
                "data": {"error": str(e)[:2000]},
                "raw": str(e)[:65536],
                "response_time_ms": elapsed_ms,
            }
            _create_poll_event(
                db, schedule, source, fail_result, outcome="on_failure",
            )
            _create_poll_event(
                db, schedule, source, fail_result,
                outcome="on_failure", type_name="always",
            )
            success = False

        schedule.last_run_at = now
        db.commit()
        return success
    finally:
        db.close()


def _resolve_poll_event_type(db, schedule, source, outcome: str, *, type_name: str | None = None):
    """Resolve EventTypeRecord for a poll outcome.

    Success uses handler_params.event_type when set, else falls back to on_success.
    Failure uses on_failure.
    When type_name is set (e.g. 'always'), look up that name only — return None if missing.
    """
    if type_name:
        return db.query(EventTypeRecord).filter(
            EventTypeRecord.source_id == source.id,
            EventTypeRecord.name == type_name,
        ).first()

    params = schedule.handler_params or {}
    if outcome == "on_failure":
        et_name = "on_failure"
    else:
        et_name = (params.get("event_type") or "").strip() or "on_success"
    return db.query(EventTypeRecord).filter(
        EventTypeRecord.source_id == source.id,
        EventTypeRecord.name == et_name,
    ).first()


def _create_poll_event(
    db, schedule, source, result: dict, *, outcome: str = "on_success",
    type_name: str | None = None,
):
    """Turn a poll result into an Event and run the pipeline.

    If type_name is set (e.g. 'always'), only emit when that event type exists
    on the source; otherwise no-op. Not pre-seeded on poll create.
    """
    event_type = _resolve_poll_event_type(
        db, schedule, source, outcome, type_name=type_name,
    )
    if type_name and event_type is None:
        return

    data = result.get("data")
    if not isinstance(data, dict):
        data = {"value": data}

    poll_meta = {
        "schedule_id": schedule.id,
        "status_code": result.get("status_code"),
        "attempts": result.get("attempts"),
        "outcome": outcome,
        "response_time_ms": result.get("response_time_ms"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if type_name:
        poll_meta["trigger"] = type_name

    normalized = {
        **data,
        "_poll": poll_meta,
        "source": source.name,
    }

    from app.ingest import ingest_event
    event = ingest_event(
        db,
        source=source,
        event_type_id=event_type.id if event_type else None,
        correlation_id=str(uuid.uuid4()),
        raw_payload=result.get("raw", "")[:65536],
        normalized_data=normalized,
        touch_last_seen=False,
    )

    try:
        evaluate_and_dispatch(db, event)
        if event.status != "failed":
            event.status = "processed"
    except Exception as e:
        event.status = "failed"
        event.processing_error = str(e)
        logger.exception(
            "Poll event pipeline failed event_id=%s schedule_id=%s",
            event.id, schedule.id,
        )

    source.last_seen_at = datetime.now(timezone.utc)
    db.commit()
