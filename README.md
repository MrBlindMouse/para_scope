# Para-Scope

Self-hosted event hub and operational dashboard for personal or small-team infrastructure. Register **sources** (webhooks and/or HTTP polls), match events with **rules**, and run **actions** (update shared fields, forward HTTP, browser push). A modular dashboard shows status, graphs, and recent activity — without replacing Prometheus/Grafana or a full workflow engine like n8n.

- **Sources** — webhook receivers and scheduled HTTP polls
- **Pipeline** — event types → rules (with conditions) → actions
- **Fields** — shared logbook / counter / value / toggle state for widgets and actions
- **Dashboard** — configurable widgets on `/`
- **Ops views** — `/events`, `/metrics`, `/system`, plus config for users, style, and audit log
- **Simple ops** — single-process FastAPI + SQLite; no Docker required

In-app detail: **Help** at `/help` after login.

[Landing Page](https://para-scope.bmd-studios.com)

## Requirements

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (or pip + venv)
- For production: systemd and nginx (optional TLS via certbot)

## Quick Start

```bash
uv venv .venv && uv pip install -r requirements.txt -p .venv
source .venv/bin/activate
cp .env.example .env   # then set PARA_SCOPE_SECRET_KEY (see below)
uvicorn app.main:app --reload
```

Open http://localhost:8000. The first start creates SQLite `para_scope.db` in the project root. With no users yet you are sent to `/setup` to create the first account; after that, use `/login`.

## Environment

Copy `.env.example` to `.env` (gitignored). Values are loaded automatically via `python-dotenv`.

| Variable | Required | Description |
|----------|----------|-------------|
| `PARA_SCOPE_SECRET_KEY` | Yes | Signs session cookies and encrypts stored secrets (Fernet). Login and secret storage fail without it. Generate with `openssl rand -hex 32`. |
| `PARA_SCOPE_SECURE_COOKIES` | No | Set to `1`/`true`/`yes` so session and CSRF cookies use the `Secure` flag (use behind HTTPS). |
| `PARA_SCOPE_DATABASE_URL` | No | SQLAlchemy URL. Default: SQLite file `para_scope.db` in the project root. |
| `PARA_SCOPE_LOG_LEVEL` | No | Logging level (default `INFO`). |
| `PARA_SCOPE_UPLOADS_DIR` | No | Directory for uploaded dashboard backgrounds (default `data/uploads`). |
| `PARA_SCOPE_VAPID_PUBLIC_KEY` | No | Web Push VAPID public key (required for `web_push` actions). |
| `PARA_SCOPE_VAPID_PRIVATE_KEY` | No | Web Push VAPID private key. |
| `PARA_SCOPE_VAPID_SUBJECT` | No | VAPID subject (`mailto:`…); defaults to `mailto:admin@localhost`. |

## First User

On a fresh install, open the app and complete `/setup`. For headless installs you can still run (after the app has created the DB once):

```bash
python create_user.py
```

## How to

Get a working pipeline after login (more detail in `/help`):

1. **Open Pipeline** at `/config/pipeline`. Add a source (name required; slug is always derived from the name). Choose type **Webhook** (optional secret in the same dialog) or **Poll** (initial schedule required). Poll sources get `on_success` and `on_failure` events automatically (no auto-rules).
2. **Add events** on the source chain (Source → Events → Rules). Once a source has producer types, webhooks must declare a matching type via the `X-Event-Type` header or body field `event_type` / `type`. Optional `always` also fires on every accepted webhook (and every poll run).
3. **Ingest an event** — pick one path:
   - **Webhook:** `POST /webhook/{slug}` with a JSON body (see [Webhooks](#webhooks)).
   - **Poll:** schedules feed `on_success` / `on_failure` into the same pipeline (see [Polling](#polling)).
4. **Add a rule** for the event(s) you care about (use **Add rule** on an event row to pre-select it). Empty conditions match all events for those types.
5. **Add an action on that rule** (`field_push` is a good smoke test; `http_forward` and `web_push` are also built in). Optional credentials can be attached in the action dialog. For `web_push`, set VAPID env vars and click the bell in the header to enable notifications. Events, rules, and actions are editable from the chain.
6. **Confirm** on `/events`, enable widgets at `/config/dashboard`, and view them on `/`.

Day-to-day: `/events`, `/metrics`, and `/system`. Manage accounts at `/config/users`.

## Polling

Set the schedule when you create or edit a **Poll** source (interval or cron, URL, timeout, retries). You can also manage schedules at `/config/source/{id}/schedules`. Jobs run in-process via APScheduler and feed the same pipeline as webhooks.

Successful polls emit `on_success` (or `handler_params.event_type` when set); failures emit `on_failure`. If you add an event type named `always` (not created automatically), it also fires on every poll run.

**Handler Params** (JSON) supports `json_path`, `event_type`, `headers`, `query`, `body`, and optional `auth_secret_id`.

## Webhooks

`POST /webhook/{slug}` accepts JSON (max body 256KB). If the source has a webhook secret, requests must include `X-Webhook-Timestamp` (unix seconds) and `X-Webhook-Signature` = HMAC-SHA256 of `{timestamp}.{raw_body}` (hex, optional `sha256=` prefix). Sources without a secret accept unsigned traffic (fine for local/dev; use a secret in production).

If you add an event type named `always` (not created automatically), it also fires on every accepted delivery. Producers keep sending their normal type; `always` is a side-emission with `_webhook.trigger` set to `always`.

Public health check: `GET /health`.

## Production (systemd + nginx)

Run a **single** uvicorn worker. Rate limits and the poll scheduler are in-process; multiple workers would split that state incorrectly. Bind uvicorn to localhost and put nginx in front for TLS and public access.

### 1. Install the app

```bash
sudo useradd --system --home /opt/para-scope --shell /usr/sbin/nologin parascope
sudo mkdir -p /opt/para-scope
sudo chown parascope:parascope /opt/para-scope

# as parascope (or clone then chown)
cd /opt/para-scope
git clone <your-repo-url> .
uv venv .venv && uv pip install -r requirements.txt -p .venv
cp .env.example .env
# edit .env — at minimum:
#   PARA_SCOPE_SECRET_KEY=<openssl rand -hex 32>
#   PARA_SCOPE_SECURE_COOKIES=1
```

### 2. systemd unit

Create `/etc/systemd/system/para-scope.service`:

```ini
[Unit]
Description=Para-Scope event dashboard
After=network.target

[Service]
Type=simple
User=parascope
Group=parascope
WorkingDirectory=/opt/para-scope
EnvironmentFile=/opt/para-scope/.env
ExecStart=/opt/para-scope/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now para-scope
sudo systemctl status para-scope
```

### 3. nginx reverse proxy

Example site config (replace `para.example.com`):

```nginx
server {
    listen 80;
    server_name para.example.com;

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
```

Enable the site, then obtain TLS (certbot will adjust the server block):

```bash
sudo ln -s /etc/nginx/sites-available/para-scope /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d para.example.com
```

### 4. First login

Open `https://para.example.com/setup` (or run `python create_user.py` from `/opt/para-scope` with the venv active). Confirm `GET https://para.example.com/health` returns OK.
