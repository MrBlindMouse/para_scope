"""Provider-specific inbound webhook verification.

This module isolates all signature/timestamp/replay logic from the main
webhook route so providers like PayPal can diverge from the local HMAC shape.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from app.models import Secret
from app.security import decrypt_secret
from app import webctx as ctx


# UI metadata for source create/edit — only fields relevant to each verifier.
WEBHOOK_PROVIDERS: list[dict[str, Any]] = [
    {
        "slug": "generic_hmac",
        "label": "Shared-secret HMAC (X-Webhook-*)",
        "summary": "Verify with X-Webhook-Timestamp and X-Webhook-Signature.",
        "secret_label": "Signing secret (optional)",
        "secret_help": "Shared HMAC secret. Leave blank to accept unsigned webhooks.",
        "secret_input_label": "Secret value",
        "secret_required": False,
        "uses_paypal_config": False,
    },
    {
        "slug": "stripe",
        "label": "Stripe (Stripe-Signature)",
        "summary": "Verify Stripe webhook signatures.",
        "secret_label": "Stripe webhook signing secret",
        "secret_help": "From the Stripe Dashboard endpoint (whsec_…). Required for verification.",
        "secret_input_label": "Signing secret",
        "secret_required": True,
        "uses_paypal_config": False,
    },
    {
        "slug": "github",
        "label": "GitHub / Gitea (X-Hub-Signature-256)",
        "summary": "Verify GitHub or Gitea webhook signatures.",
        "secret_label": "Webhook secret",
        "secret_help": "The secret configured on the GitHub/Gitea webhook.",
        "secret_input_label": "Secret value",
        "secret_required": True,
        "uses_paypal_config": False,
    },
    {
        "slug": "slack",
        "label": "Slack (X-Slack-Signature)",
        "summary": "Verify Slack request signatures.",
        "secret_label": "Signing secret",
        "secret_help": "Slack app Signing Secret from Basic Information.",
        "secret_input_label": "Signing secret",
        "secret_required": True,
        "uses_paypal_config": False,
    },
    {
        "slug": "discord",
        "label": "Discord (Ed25519)",
        "summary": "Verify Discord interactions with Ed25519.",
        "secret_label": "Application public key",
        "secret_help": "Discord application Public Key (hex), not a bot token.",
        "secret_input_label": "Public key",
        "secret_required": True,
        "uses_paypal_config": False,
    },
    {
        "slug": "paypal",
        "label": "PayPal (verify-webhook-signature)",
        "summary": "Verify via PayPal’s verify-webhook-signature API.",
        "secret_label": "Client secret",
        "secret_help": "PayPal app client secret (stored encrypted).",
        "secret_input_label": "Client secret",
        "secret_required": True,
        "uses_paypal_config": True,
    },
]


def get_webhook_providers() -> list[dict[str, Any]]:
    return list(WEBHOOK_PROVIDERS)


def get_webhook_provider_slugs() -> set[str]:
    return {p["slug"] for p in WEBHOOK_PROVIDERS}


def get_webhook_provider(slug: str) -> dict[str, Any] | None:
    for provider in WEBHOOK_PROVIDERS:
        if provider["slug"] == slug:
            return provider
    return None


class WebhookAuthError(Exception):
    """Raised by verifier functions to return a structured HTTP error."""

    def __init__(self, status_code: int, payload: dict[str, Any]):
        super().__init__(payload.get("error") or "Webhook auth failed")
        self.status_code = status_code
        self.payload = payload


def _resolve_webhook_provider(source) -> str:
    provider = (source.config or {}).get("webhook_provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    return "generic_hmac"


def _require_source_secret(db, source, *, label: str = "Webhook secret") -> str:
    if not source.webhook_secret_id:
        raise WebhookAuthError(401, {"error": f"{label} not configured"})
    secret = db.query(Secret).filter(Secret.id == source.webhook_secret_id).first()
    if not secret:
        raise WebhookAuthError(401, {"error": "Webhook secret not configured"})
    return decrypt_secret(secret.encrypted_value)


def _touch_replay_cache(*, source_id: int, replay_key: str) -> None:
    now = time.time()
    ctx._cleanup_replay_cache(now - ctx._WEBHOOK_REPLAY_TTL_SECONDS)
    ctx._WEBHOOK_REPLAY_CACHE[replay_key] = now


def _check_replay_cache(*, source_id: int, replay_key: str) -> bool:
    now = time.time()
    ctx._cleanup_replay_cache(now - ctx._WEBHOOK_REPLAY_TTL_SECONDS)
    return replay_key in ctx._WEBHOOK_REPLAY_CACHE


@dataclass(frozen=True)
class VerifiedWebhook:
    signed: bool


def verify_webhook_request(*, db, source, request, raw_body: bytes) -> VerifiedWebhook:
    """Verify the incoming request and return whether it's signed/verified."""

    provider = _resolve_webhook_provider(source)
    if provider == "generic_hmac":
        return _verify_generic_hmac(db=db, source=source, request=request, raw_body=raw_body)
    if provider == "stripe":
        return _verify_stripe(db=db, source=source, request=request, raw_body=raw_body)
    if provider == "github":
        return _verify_github(db=db, source=source, request=request, raw_body=raw_body)
    if provider == "slack":
        return _verify_slack(db=db, source=source, request=request, raw_body=raw_body)
    if provider == "discord":
        return _verify_discord(db=db, source=source, request=request, raw_body=raw_body)
    if provider == "paypal":
        return _verify_paypal_postback(db=db, source=source, request=request, raw_body=raw_body)

    raise WebhookAuthError(400, {"error": f"Unknown webhook provider '{provider}'"})


def _verify_generic_hmac(*, db, source, request, raw_body: bytes) -> VerifiedWebhook:
    signature = (request.headers.get("x-webhook-signature") or "").strip()
    actual = signature.replace("sha256=", "") if signature else ""
    timestamp_str = (request.headers.get("x-webhook-timestamp") or "").strip()

    # Signed flow (shared secret + timestamp + replay protection).
    if source.webhook_secret_id:
        secret = _require_source_secret(db, source)

        if not timestamp_str:
            raise WebhookAuthError(
                400,
                {"error": "Timestamp required", "hint": "Send X-Webhook-Timestamp (unix seconds)"},
            )
        try:
            ts = float(timestamp_str)
        except ValueError:
            raise WebhookAuthError(400, {"error": "Invalid timestamp"})

        now = time.time()
        if abs(now - ts) > ctx._WEBHOOK_REPLAY_TTL_SECONDS:
            raise WebhookAuthError(400, {"error": "Timestamp expired"})

        signed_payload = f"{timestamp_str}.".encode() + raw_body
        expected = hmac.new(
            secret.encode(),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, actual):
            raise WebhookAuthError(401, {"error": "Invalid signature"})

        replay_key = f"{source.id}:{actual}"
        if _check_replay_cache(source_id=source.id, replay_key=replay_key):
            raise WebhookAuthError(409, {"error": "Duplicate request"})
        _touch_replay_cache(source_id=source.id, replay_key=replay_key)

        return VerifiedWebhook(signed=True)

    # Unsigned flow (optional soft replay check if timestamp header is sent).
    if timestamp_str:
        try:
            ts = float(timestamp_str)
        except ValueError:
            raise WebhookAuthError(400, {"error": "Invalid timestamp"})
        now = time.time()
        if abs(now - ts) > ctx._WEBHOOK_REPLAY_TTL_SECONDS:
            raise WebhookAuthError(400, {"error": "Timestamp expired"})

        replay_key = f"{source.id}:{ts}"
        if _check_replay_cache(source_id=source.id, replay_key=replay_key):
            raise WebhookAuthError(409, {"error": "Duplicate request"})
        _touch_replay_cache(source_id=source.id, replay_key=replay_key)

    return VerifiedWebhook(signed=False)


def _verify_stripe(*, db, source, request, raw_body: bytes) -> VerifiedWebhook:
    secret = _require_source_secret(db, source, label="Stripe signing secret")

    sig_header = (request.headers.get("Stripe-Signature") or "").strip()
    if not sig_header:
        raise WebhookAuthError(400, {"error": "Missing Stripe-Signature header"})

    ts_str: str | None = None
    v1_sigs: list[str] = []
    for part in sig_header.split(","):
        part = part.strip()
        if part.startswith("t="):
            ts_str = part[2:].strip()
        elif part.startswith("v1="):
            v1_sigs.append(part[3:].strip())

    if not ts_str or not v1_sigs:
        raise WebhookAuthError(400, {"error": "Invalid Stripe-Signature header"})

    try:
        ts = float(ts_str)
    except ValueError:
        raise WebhookAuthError(400, {"error": "Invalid timestamp"})

    now = time.time()
    if abs(now - ts) > ctx._WEBHOOK_REPLAY_TTL_SECONDS:
        raise WebhookAuthError(400, {"error": "Timestamp expired"})

    signed_payload = ts_str.encode() + b"." + raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    actual = v1_sigs[0] if v1_sigs else ""

    # Stripe may include multiple v1 signatures. Accept any match.
    if not any(hmac.compare_digest(expected, s) for s in v1_sigs):
        raise WebhookAuthError(401, {"error": "Invalid signature"})

    replay_key = f"{source.id}:stripe:{ts_str}:{actual}"
    if _check_replay_cache(source_id=source.id, replay_key=replay_key):
        raise WebhookAuthError(409, {"error": "Duplicate request"})
    _touch_replay_cache(source_id=source.id, replay_key=replay_key)
    return VerifiedWebhook(signed=True)


def _verify_github(*, db, source, request, raw_body: bytes) -> VerifiedWebhook:
    secret = _require_source_secret(db, source, label="GitHub signing secret")
    header = (request.headers.get("X-Hub-Signature-256") or "").strip()
    if not header:
        raise WebhookAuthError(400, {"error": "Missing X-Hub-Signature-256 header"})

    actual = header.replace("sha256=", "")
    signed_payload = raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, actual):
        raise WebhookAuthError(401, {"error": "Invalid signature"})

    replay_key = f"{source.id}:github:{actual}"
    if _check_replay_cache(source_id=source.id, replay_key=replay_key):
        raise WebhookAuthError(409, {"error": "Duplicate request"})
    _touch_replay_cache(source_id=source.id, replay_key=replay_key)
    return VerifiedWebhook(signed=True)


def _verify_slack(*, db, source, request, raw_body: bytes) -> VerifiedWebhook:
    secret = _require_source_secret(db, source, label="Slack signing secret")

    signature = (request.headers.get("X-Slack-Signature") or "").strip()
    timestamp_str = (request.headers.get("X-Slack-Request-Timestamp") or "").strip()
    if not signature or not timestamp_str:
        raise WebhookAuthError(400, {"error": "Missing Slack signature headers"})

    try:
        ts = float(timestamp_str)
    except ValueError:
        raise WebhookAuthError(400, {"error": "Invalid timestamp"})

    now = time.time()
    if abs(now - ts) > ctx._WEBHOOK_REPLAY_TTL_SECONDS:
        raise WebhookAuthError(400, {"error": "Timestamp expired"})

    actual = signature.replace("v0=", "")
    signed_payload = b"v0:" + timestamp_str.encode() + b":" + raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, actual):
        raise WebhookAuthError(401, {"error": "Invalid signature"})

    replay_key = f"{source.id}:slack:{timestamp_str}:{actual}"
    if _check_replay_cache(source_id=source.id, replay_key=replay_key):
        raise WebhookAuthError(409, {"error": "Duplicate request"})
    _touch_replay_cache(source_id=source.id, replay_key=replay_key)
    return VerifiedWebhook(signed=True)


def _verify_discord(*, db, source, request, raw_body: bytes) -> VerifiedWebhook:
    public_key_hex = _require_source_secret(db, source, label="Discord public key")

    signature = (request.headers.get("X-Signature-Ed25519") or "").strip()
    timestamp = (request.headers.get("X-Signature-Timestamp") or "").strip()
    if not signature or not timestamp:
        raise WebhookAuthError(400, {"error": "Missing Discord signature headers"})

    try:
        ts = float(timestamp)
    except ValueError:
        raise WebhookAuthError(400, {"error": "Invalid timestamp"})

    now = time.time()
    if abs(now - ts) > ctx._WEBHOOK_REPLAY_TTL_SECONDS:
        raise WebhookAuthError(400, {"error": "Timestamp expired"})

    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        verify_key.verify(timestamp.encode() + raw_body, bytes.fromhex(signature))
    except (BadSignatureError, ValueError):
        raise WebhookAuthError(401, {"error": "Invalid signature"})

    replay_key = f"{source.id}:discord:{signature}"
    if _check_replay_cache(source_id=source.id, replay_key=replay_key):
        raise WebhookAuthError(409, {"error": "Duplicate request"})
    _touch_replay_cache(source_id=source.id, replay_key=replay_key)
    return VerifiedWebhook(signed=True)


_PAYPAL_TOKEN_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def _paypal_env_base(env: str) -> tuple[str, str]:
    env = (env or "").strip().lower() or "sandbox"
    if env in ("prod", "production", "live"):
        return (
            "https://api-m.paypal.com",
            "https://api-m.paypal.com/v1/oauth2/token",
        )
    return (
        "https://api-m.sandbox.paypal.com",
        "https://api-m.sandbox.paypal.com/v1/oauth2/token",
    )


def _paypal_get_access_token(*, client_id: str, client_secret: str, token_url: str) -> str:
    # ponytail: simple in-memory cache; upgrade path is DB-backed or shared cache.
    cache_key = (token_url, client_id)
    now = time.time()
    cached = _PAYPAL_TOKEN_CACHE.get(cache_key)
    if cached and cached.get("expires_at", 0) > now + 15:
        return cached["access_token"]

    payload = {"grant_type": "client_credentials"}
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    with httpx.Client(timeout=20) as client:
        resp = client.post(token_url, data=payload, headers=headers, auth=(client_id, client_secret))
        resp.raise_for_status()
        data = resp.json()
    access_token = data["access_token"]
    expires_in = float(data.get("expires_in") or 3600)
    _PAYPAL_TOKEN_CACHE[cache_key] = {
        "access_token": access_token,
        "expires_at": now + expires_in,
    }
    return access_token


def _verify_paypal_postback(*, db, source, request, raw_body: bytes) -> VerifiedWebhook:
    cfg = source.config or {}
    paypal_webhook_id = str(cfg.get("paypal_webhook_id") or "").strip()
    paypal_client_id = str(cfg.get("paypal_client_id") or "").strip()
    paypal_env = str(cfg.get("paypal_environment") or "sandbox").strip()
    if not paypal_webhook_id or not paypal_client_id:
        raise WebhookAuthError(400, {"error": "PayPal Webhook ID and Client ID are required in Source config"})

    token_base, token_url = _paypal_env_base(paypal_env)
    secret = _require_source_secret(db, source, label="PayPal client secret")

    # PayPal verification request needs multiple headers.
    auth_algo = (request.headers.get("PAYPAL-AUTH-ALGO") or "").strip()
    cert_url = (request.headers.get("PAYPAL-CERT-URL") or "").strip()
    transmission_id = (request.headers.get("PAYPAL-TRANSMISSION-ID") or "").strip()
    transmission_sig = (request.headers.get("PAYPAL-TRANSMISSION-SIG") or "").strip()
    transmission_time = (request.headers.get("PAYPAL-TRANSMISSION-TIME") or "").strip()

    missing = [
        name
        for name, value in (
            ("PAYPAL-AUTH-ALGO", auth_algo),
            ("PAYPAL-CERT-URL", cert_url),
            ("PAYPAL-TRANSMISSION-ID", transmission_id),
            ("PAYPAL-TRANSMISSION-SIG", transmission_sig),
            ("PAYPAL-TRANSMISSION-TIME", transmission_time),
        )
        if not value
    ]
    if missing:
        raise WebhookAuthError(400, {"error": f"Missing PayPal verification headers: {', '.join(missing)}"})

    replay_key = f"{source.id}:paypal:{transmission_id}"
    if _check_replay_cache(source_id=source.id, replay_key=replay_key):
        raise WebhookAuthError(409, {"error": "Duplicate request"})

    try:
        webhook_event = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        # Keep consistent with the main route error.
        raise WebhookAuthError(400, {"error": "Invalid JSON"})

    access_token = _paypal_get_access_token(
        client_id=paypal_client_id,
        client_secret=secret,
        token_url=token_url,
    )

    verify_url = f"{token_base}/v1/notifications/verify-webhook-signature"
    payload = {
        "auth_algo": auth_algo,
        "cert_url": cert_url,
        "transmission_id": transmission_id,
        "transmission_sig": transmission_sig,
        "transmission_time": transmission_time,
        "webhook_id": paypal_webhook_id,
        "webhook_event": webhook_event,
    }

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            verify_url,
            json=payload,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    verification_status = (data.get("verification_status") or "").strip().upper()
    if verification_status not in ("SUCCESS", "VALID"):
        raise WebhookAuthError(401, {"error": "Invalid signature"})

    _touch_replay_cache(source_id=source.id, replay_key=replay_key)
    return VerifiedWebhook(signed=True)

