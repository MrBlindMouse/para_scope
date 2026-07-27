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
5. **Add an action on that rule** (`field_push` is a good smoke test; `http_forward`, `notify`, and `web_push` are also built in; `local_script` needs `PARA_SCOPE_ALLOW_LOCAL_ACTIONS=1`). Optional credentials can be attached in the action dialog. For `web_push`, set VAPID env vars and click the bell in the header to enable notifications. Events, rules, and actions are editable from the chain.
6. **Confirm** on `/events`, enable widgets at `/config/dashboard`, and view them on `/`.

Day-to-day: `/events`, `/metrics`, and `/system`. Manage accounts at `/config/users`.

## Polling

Set the schedule when you create or edit a **Poll** source (exactly one schedule per poll source). Polls are organized as:

- **Category** — URL / HTTP, System, Connectivity / Reachability, Storage / Filesystem, Application / Domain, External
- **Subtype** — the specific poller that runs, for example `http_get`, `system_snapshot`, `dns_resolve`, `backup_age`, or `rss_atom_change`

Jobs run in-process via APScheduler and feed the same pipeline as webhooks. Use **Run now** on a poll source to fire immediately.

Successful polls emit `on_success` by default; failures emit `on_failure`. Some pollers also let you set a different success event name. If you add an event type named `always` (not created automatically), it also fires on every poll run.

### Poll privileges

Para-Scope does **not** need to run as root by default. The normal production setup is:

- run the app as a dedicated `parascope` service user
- grant that user only the filesystem and group access needed for the pollers you actually configure

For the shipped pollers:

- **Usually fine as an unprivileged service user:** URL / HTTP, DNS, TCP connect, TLS cert expiry, RSS / Atom, public HTTP status, local LLM HTTP status, database health, system snapshot, disk free space, backup age, git status
- **May need extra read access:** log pattern watch, backup age on restricted paths, disk free space on restricted mount points
- **May need host group access:** `journal_recent_errors` often needs membership in `systemd-journal` or `adm` (distro-dependent) so `journalctl` can read the system journal
- **Usually works unprivileged, but still depends on host policy:** `systemd_failed_units` (it shells out to `systemctl`)

Highest reasonable privilege for the shipped pollers is usually **a dedicated service user with a minimal supplemental group set and/or ACLs on the monitored paths**, not root.

## Webhooks

`POST /webhook/{slug}` accepts JSON (max body 256KB). If the source has a webhook secret, requests must include `X-Webhook-Timestamp` (unix seconds) and `X-Webhook-Signature` = HMAC-SHA256 of `{timestamp}.{raw_body}` (hex, optional `sha256=` prefix). Sources without a secret accept unsigned traffic (fine for local/dev; use a secret in production).

If you add an event type named `always` (not created automatically), it also fires on every accepted delivery. Producers keep sending their normal type; `always` is a side-emission with `_webhook.trigger` set to `always`.

Public health check: `GET /health`.

## Production (VM + systemd + nginx)

Run a **single** uvicorn worker. Rate limits and the poll scheduler are in-process; multiple workers would split that state incorrectly. Bind uvicorn to localhost and put nginx in front for TLS and public access.

This section assumes a small Linux VM (Debian/Ubuntu-style layout), a DNS name such as `para.example.com`, and systemd + nginx on the host.

### 1. VM prep

Create the VM, point DNS at it, then install the base packages you need:

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y git curl nginx certbot python3-certbot-nginx
```

Install `uv` if it is not already present:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
```

Recommended host checklist:

- open inbound `80/tcp` and `443/tcp` (plus `22/tcp` for SSH)
- create an `A` / `AAAA` record for your VM before running certbot
- use a real sudo-capable admin account for provisioning; do **not** run the app itself as that account

### 2. Create the service user and app directory

```bash
sudo useradd --system --create-home --home-dir /opt/para-scope --shell /usr/sbin/nologin parascope
sudo mkdir -p /opt/para-scope
sudo chown parascope:parascope /opt/para-scope
```

### 3. Optional host access for pollers

Start least-privileged and add only what you need:

```bash
# journal access on many distros (pick the group your distro uses)
sudo usermod -aG systemd-journal parascope
# or
sudo usermod -aG adm parascope
```

For log files, backup directories, or repositories outside `/opt/para-scope`, prefer group ownership or ACLs over running the service as root:

```bash
# example: allow parascope to read a specific log tree
sudo setfacl -R -m u:parascope:rx /var/log/myapp
sudo setfacl -R -d -m u:parascope:rx /var/log/myapp
```

Notes:

- `journal_recent_errors` may need journal-reading group access
- `backup_age`, `disk_free_space`, `log_pattern_watch`, and `git_status` only work where the service user can traverse/read the target paths
- root is **not** the recommended default, even for system/storage polls

### 4. Install the app

Run the app install steps as the `parascope` user:

```bash
sudo -u parascope -H bash -lc '
set -e
cd /opt/para-scope
git clone <your-repo-url> .
uv venv .venv
uv pip install -r requirements.txt -p .venv
cp .env.example .env
'
```

Then edit `/opt/para-scope/.env` and set at minimum:

```dotenv
PARA_SCOPE_SECRET_KEY=<openssl rand -hex 32>
PARA_SCOPE_SECURE_COOKIES=1
PARA_SCOPE_LOG_LEVEL=INFO
```

Generate the secret key with:

```bash
openssl rand -hex 32
```

### 5. systemd unit

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
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now para-scope
sudo systemctl status para-scope
sudo journalctl -u para-scope -f
```

### 6. nginx reverse proxy

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
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
```

Save it as `/etc/nginx/sites-available/para-scope`, then enable and validate it:

```bash
sudo ln -s /etc/nginx/sites-available/para-scope /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Then obtain TLS (certbot will adjust the server block):

```bash
sudo certbot --nginx -d para.example.com
```

### 7. First start and first login

Confirm the local app is reachable through nginx:

```bash
curl -I http://127.0.0.1:8000/health
curl -I https://para.example.com/health
```

Then open `https://para.example.com/setup` and create the first user.

For headless installs, first make sure the app has started once so the DB exists, then run:

```bash
cd /opt/para-scope
source .venv/bin/activate
python create_user.py
```

### 8. Ongoing operations

- App logs: `sudo journalctl -u para-scope -f`
- Restart after upgrades or env changes: `sudo systemctl restart para-scope`
- Reload nginx after config changes: `sudo nginx -t && sudo systemctl reload nginx`
- Health check: `GET /health`

### 9. Upgrade flow

```bash
sudo systemctl stop para-scope
sudo -u parascope -H bash -lc '
set -e
cd /opt/para-scope
git pull --ff-only
uv pip install -r requirements.txt -p .venv
'
sudo systemctl start para-scope
sudo systemctl status para-scope
```
