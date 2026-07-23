# Para-Scope

Modular event dashboard for personal or small-team infrastructure.

## Quick Start

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
cp .env.example .env   # then set PARA_SCOPE_SECRET_KEY (see below)
uvicorn app.main:app --reload
```

Open http://localhost:8000 — with no users yet you are sent to `/setup` to create the first account. After that, use `/login`.

## Environment

Copy `.env.example` to `.env` (gitignored). Values are loaded automatically via `python-dotenv`.

| Variable | Required | Description |
|----------|----------|-------------|
| `PARA_SCOPE_SECRET_KEY` | Yes | Signs session cookies and encrypts stored secrets (Fernet). Login and secret storage fail without it. Generate with `openssl rand -hex 32`. |
| `PARA_SCOPE_SECURE_COOKIES` | No | Set to `1`/`true`/`yes` so session and CSRF cookies use the `Secure` flag (HTTPS). |
| `PARA_SCOPE_VAPID_PUBLIC_KEY` | No | Web Push VAPID public key (required for `web_push` actions). |
| `PARA_SCOPE_VAPID_PRIVATE_KEY` | No | Web Push VAPID private key. |
| `PARA_SCOPE_VAPID_SUBJECT` | No | VAPID subject (`mailto:`…); defaults to `mailto:admin@localhost`. |

## First User

On a fresh install, open the app and complete `/setup`. For headless installs you can still run:

```bash
python create_user.py
```

## How to

Get a working pipeline after login:

1. **Open Pipeline** at `/config/pipeline`. Add a source (name required; slug is always derived from the name). Choose type **Webhook** (optional secret in the same dialog) or **Poll** (initial schedule required). Poll sources get `on_success` and `on_failure` events automatically (no auto-rules).
2. **Add events** on the source chain (Source → Events → Rules). Once a source has types, webhooks must declare a matching type via the `X-Event-Type` header or body field `event_type` / `type`.
3. **Ingest an event** — pick one path:
   - **Webhook:** `POST /webhook/{slug}` with a JSON body (see [Webhooks](#webhooks)).
   - **Poll:** schedule runs feed `on_success` / `on_failure` into the same pipeline (see [Polling](#polling)).
4. **Add a rule** for the event(s) you care about (use **Add rule** on an event row to pre-select it). Empty conditions `{}` match all events for those types.
5. **Add an action on that rule** (`field_push` is a good smoke test; `http_forward` and `web_push` are also built in). Optional credentials can be attached in the action dialog. For `web_push`, set VAPID env vars and click **Enable notifications** in the header. Events, rules, and actions are all editable from the chain.
6. **Confirm** on `/events`, enable widgets at `/config/dashboard`, and view them on `/`.

Day-to-day: `/events`, `/metrics`, and `/system`. Manage accounts at `/config/users`.

## Polling

For step 3 above: create a schedule under a source (`/config/source/{id}/schedules`). Interval or cron jobs run in-process via APScheduler and feed events into the same pipeline as webhooks. Successful polls emit `on_success` (or `handler_params.event_type` when set); failures emit `on_failure`. If you add an event named `always` (not seeded by default), it also fires on every poll run.

`handler_params` JSON supports `json_path`, `event_type`, `headers`, `query`, `body`, and optional `auth_secret_id`.

## Webhooks

For step 3 above: `POST /webhook/{slug}` accepts JSON. If the source has a linked webhook secret, requests must include `X-Webhook-Timestamp` (unix seconds) and `X-Webhook-Signature` = HMAC-SHA256 of `{timestamp}.{raw_body}` (hex, optional `sha256=` prefix). Sources without a secret accept unsigned traffic (fine for local/dev; use a secret in production).
