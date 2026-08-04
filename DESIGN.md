Version: 0.1.1  

Date: 2026-07-17 (updated 2026-07-31: v0.1.1)

Status: Living design. **AGENTS.md** is the ops contract for agents; this document is vision plus an explicit shipped-vs-planned split below. Releases: git tags + [CHANGELOG.md](CHANGELOG.md). Trunk-based on `main`; SemVer; wipe/recreate over migrations (see README Versioning).

### 0. Shipped in v0.1 vs Planned

**Shipped (matches the codebase today):**

- Single-process FastAPI + SQLite (`create_all` only; no Alembic / no schema patches)
- Cookie sessions, CSRF, Fernet-encrypted secrets, login rate limit
- Sources (webhook + poll), event types, one polling schedule per poll source, rules, actions (`field_push`, `http_forward`, `notify`, `web_push`, `local_script`)
- Poll categories + typed poller subtypes for URL/HTTP, system, connectivity, storage, application, and external checks
- Field sinks (logbook / value / text / toggle / data) + AuditLog + DashboardLayout widgets
- Webhook HMAC when a secret is configured (`X-Webhook-Timestamp` + HMAC over `{timestamp}.{body}`); unsigned sources allowed for local/dev
- In-memory login and webhook rate limits (single-process ceiling)
- In-process APScheduler pollers; HTMX dashboard refresh; SSE live event tail on `/events/stream`
- Action/poller registries in-process (not a plugin directory)
- Shared Fields as first-class sinks used by both actions and widgets
- Dashboard Triggers widget + `trigger_source` action; self-metrics on `/system`
- Vanilla CSS (37signals/Fizzy-inspired, OKLCH tokens, cascade layers)
- GridStack layout + ApexCharts widgets
- Web Push via VAPID (optional env keys)
- systemd + nginx production path documented; pure git + venv primary distribution

**Planned / not in v0.1 (do not treat as implemented):**

- Phase-2 poll integrations: Docker/runtime snapshots, SMART health, ZFS/Btrfs pool health, MQTT broker checks, deeper queue integrations
- Mandatory webhook secrets in production (operator guidance only today)
- Heatmap / calendar heatmap / range-column widgets; markdown notes display

---

### 1. Vision and Purpose

A self-hosted, single-process Python application that acts as a unified event hub and operational dashboard for personal or small-team infrastructure and projects.

Users register **Sources**. Each source can emit events via verified webhooks or be actively polled on a configurable schedule. Incoming events are normalized, filtered by **Rules** (with conditions), and routed to one or more **Actions**. Shared **Fields** act as durable sinks (logbooks, values, text, toggles). A modular web dashboard provides status tiles, graphs, event logs, and configuration surfaces.

The authenticated dashboard **Triggers** widget and the `trigger_source` action can fire a poll (Run now) or ingest a webhook event type with an optional templated payload. They do not introduce a third source type or make a loopback HTTP request. Nested trigger cascades are capped at depth 3.

Dashboard templates use bare Field slugs (`{{ temperature.value }}`), including widget titles, labels, units, and link URLs at display time (notes stay literal). Pipeline templates and rule condition keys use event payload paths plus reserved `fields.<slug>.<path>`; Update Field also exposes reserved `field`. Field slugs cannot be any name in `RESERVED_FIELD_SLUGS` (`field`, `fields`, `value`, `source`, `_poll`, `dt`, `system`, `ts`).

The system prioritizes:

- Modularity and extensibility
- Privacy and self-hosting (no mandatory external services)
- Simplicity of operation (pure git repository, no Docker required)
- General usefulness beyond the original author’s stack so others can adopt and extend it

It is deliberately *not* a full observability platform (Prometheus/Grafana replacement), a general workflow engine (n8n/Temporal replacement), or a heavy multi-tenant SaaS product.

### 2. Goals

- Clean registration and lifecycle management of heterogeneous sources.
- Reliable, verified webhook ingestion with custom event types per source.
- Flexible polling as a first-class peer to webhooks.
- Composable actions triggered by events (with simple field-match conditions).
- Persistent event history + aggregated metrics suitable for graphs and audit logs.
- Attractive, modular, authenticated web dashboard built with FastAPI + Jinja2 + HTMX + vanilla CSS.
- Strong authentication and secret handling.
- Easy local development and deployment from a pure git repository.
- Clear extension points so new pollers and actions can be added without core changes.
- Reasonable defaults and progressive complexity so a single user can start quickly while power users can go deep.

### 3. Non-Goals

- Multi-region high-availability or horizontal scaling as a primary concern (single-node / small-team first).
- Built-in long-term metrics storage that competes with dedicated time-series databases.
- Visual workflow designer or complex branching logic (keep the action model relatively linear + filters).
- Native mobile apps (responsive web is sufficient).
- Mandatory cloud dependencies or telemetry.
- Docker / container-first packaging (git + Python environment is the primary distribution method).
- PostgreSQL support or migration tooling; SQLite + wipe/recreate remains the storage contract.
- Alternative authentication stacks, API tokens, or user roles; authenticated users retain full access.
- Stateful rule rate limits or time windows; conditions remain simple payload matching.
- Configuration import/export UI; backups remain the SQLite database plus its matching secret key.
- Durable queues, dead-letter processing, or delivery guarantees; actions remain immediate and in-process.
- Plugin discovery or Python entry-point loading; extensions stay built into the app through its existing registries.

### 4. Core Domain Concepts

**Source**  

A registered origin of events. Examples: Flit PKM, trading bots, e-commerce/payment providers, Uptime Kuma, custom services, system metrics collectors, etc.

Shipped and intended `source_type` values: `webhook` | `poll`. Dashboard controls reuse an existing webhook source/event type.

A source owns:

- Identity and metadata (name, slug, description)
- Authentication / verification material for webhooks (optional secret)
- Zero or more webhook event type definitions
- Exactly one polling schedule when `source_type` is poll (none for webhooks)
- Associated secrets (API keys, tokens) used by pollers or actions
- Enabled flag and last-seen timestamp

**Event Type**  

A named kind of occurrence belonging to a source (e.g. `on_success`, `on_failure`, `client.created`, `monitor.down`).  

Poll sources automatically receive `on_success` / `on_failure`. An optional `always` type can be added on poll or webhook sources and fires on every poll run / accepted webhook.

**Event**  

A concrete occurrence. Normalized internal representation plus the original payload. Immutable once accepted. Carries processing status (`pending` | `processed` | `failed`).

**Polling Schedule**  

A timing/job row attached to a poll source: interval, cron, or **never** (trigger-only — no APScheduler tick; Run now / `trigger_source` only). Handler type (for example `http_get`, `system_snapshot`, `dns_resolve`, `backup_age`), typed handler parameters, timeout, and retry count. Each poll source has exactly one schedule. Jobs run in-process via APScheduler with jitter and consecutive-failure backoff.

**Action**  

A side-effect attached to rules. Built-in types:

- `field_push` — write to a shared Field (logbook append, value ops, text template, toggle Fixed/Switch); skips when the computed value is unchanged. Logbook **Value from event** (and data **Object from event**) resolves a dotted path, a safe maths expression (`+ - * / %`, `abs`, `round`, `min`, `max`, `trunc`, `sum`, `avg`), or a JSON-like shape into a typed value. Aggregates (`sum` / `avg` / `min` / `max`) accept scalars, a path to a flat `number[]` (e.g. `sum(profit_loss)`), or leaves collected with list segment `**` (e.g. `sum(bars.**.pl)`); path-only `bars.**.pl` returns that list. List segment `*` remains one row (matched by rule conditions, else index 0) — never “all rows”. Shape keys are quoted (may use `{{ }}`); bare values are path/maths; quoted string values are literal unless they contain `{{ }}` (whole-leaf → typed; mixed → string). Elsewhere, **Templates** (`{{ }}`) are string interpolation only (actions and text fields). Pipeline actions and rule conditions may read any Field via `fields.<slug>.<path>`.
- `http_forward` — outbound HTTP request (templated URL/headers/body, auth secrets, presets for ntfy/Gotify/Discord, optional HMAC)
- `notify` — thin convenience wrapper over HTTP for ntfy / Gotify / Discord (title/body templates)
- `web_push` — browser push notification via VAPID
- `local_script` — run a local command or argv list (gated by `PARA_SCOPE_ALLOW_LOCAL_ACTIONS`; optional path allowlist)
- `trigger_source` — run a poll once (Run now) or ingest a webhook event type with an optional templated payload; nested cascades capped at depth 3

Actions are dispatched by rules. Rules support field-match conditions (exact, not, gt/lt, contains, regex) on dotted paths, including `fields.<slug>.…`. List segment `*` means “any element” in conditions (correlated across fields that share the same list); when those conditions match, the same indexes apply to `*` in that rule’s action/template paths. Without starred conditions, `*` is the first element. Stateful rate limits and time windows are intentionally outside the rule language.

**Rule**  

The binding of event type(s) + optional conditions → one or more actions (ordered by `order_index`).

**Field**  

Global named sink used by both actions and widgets:

- `logbook` — append-only entries (pruned to max_entries); history for charts/series
- `value` — numeric current value only (increment / decrement / set / reset; no time-series)
- `text` — string state (template)
- `toggle` — boolean state (Fixed or Switch)

**Dashboard View / Widget**  

Modular visualization or control surface (status, charts, series, links, notes, system info, display). Layout is stored in `DashboardLayout` and edited via GridStack. Widgets refresh via HTMX.

Series displays (logbook): `line` / `area` / `column` with per-style options (e.g. stepline, stacked, horizontal). Chart displays (value/text): `pie` / `radial` / `radar` / `polar`. Radial styles use an explicit max/target. Notes display: `Text` (plain body in layout config, debounced save from the dashboard).

ponytail: heatmap, calendar heatmap, and range columns — To be implemented (need grid / min-max data shapes).
ponytail: notes `Markdown` display — To be implemented (rendered Markdown alongside plain Text).

### 5. High-Level Architecture

Single primary process (FastAPI application) that hosts:

- HTTP server (dashboard + webhook ingress endpoints)
- Background scheduler for polling jobs (APScheduler)
- Event processing pipeline (receive → normalize → filter → execute actions → persist)
- In-process background work (intentional; no durable queue)
- Auth middleware and session management
- Static file serving for CSS, HTMX, ApexCharts, GridStack

Storage is SQLite-only for zero-config simplicity. PostgreSQL and migration tooling are not planned.

Runtime configuration (sources, rules, schedules, dashboard layouts, fields) lives in the database. The git repository contains the application code, templates, static assets, and documentation.

Data flow (simplified):

1. External system → Webhook POST (verified when secret configured) **or** internal poller runs
2. Ingress / Poller produces a raw result
3. Normalization produces a canonical Event (via `ingest_event`)
4. Rule engine evaluates matching rules and conditions (`evaluate_and_dispatch`)
5. Matching Actions are executed immediately in-process
6. Event and any derived metrics / field updates are persisted
7. Dashboard and HTMX-polled feeds consume the stores

### 6. Detailed Component Responsibilities

**Source Registry &amp; Management**  

CRUD for sources, event type registration, secret association, enable/disable, health tracking. Provides the admin UI surface at `/config/pipeline` and related routes.

**Webhook Ingress**  

`POST /webhook/{slug}`.  

Responsibilities:

- Signature verification when a webhook secret is configured (`X-Webhook-Timestamp` + HMAC-SHA256 of `{timestamp}.{raw_body}`)
- Body size limit (256 KB)
- Event type extraction (known event-type headers such as `X-Event-Type` / `X-GitHub-Event` / `X-Contentful-Topic`, or body `event_type` / `type`)
- Optional side-emission of an `always` event type when present on the source
- Immediate processing into the shared pipeline
- Clear error responses

Unsigned sources are accepted (convenient for local/dev).

**Polling Engine**  

APScheduler-backed. Each schedule knows its source, interval/cron, handler type, and typed parameters.  

Handlers are registered via `register_poller`, with metadata for category, label, summary, and config fields. Shipped handlers cover URL/HTTP, system snapshots, systemd/journal checks, connectivity probes, storage checks, application/domain checks, and a small external-integration set.

Jobs report success/failure, latency, and extracted data back into the same pipeline as webhooks. Interval schedules receive jitter and consecutive-failure backoff.

**Event Pipeline**  

- Acceptance and normalization (`app/ingest.py`)
- Rule matching and condition evaluation (`app/pipeline.py`)
- Action dispatch
- Persistence of the event and side-effect outcomes
- Status tracking on the Event itself

**Action Engine**  

Registry of action types (`register_action`). Each action receives the normalized event + its own configuration.  

Built-in actions cover field updates, HTTP egress, and Web Push. Custom actions are added via the same in-process registration.

**Storage Layer**  

- Event log (queryable by time, source, type, correlation ID)
- Field state + FieldLogEntry (for logbooks; series/charts use Fields)
- AuditLog

Schema is models + `Base.metadata.create_all()` (no Alembic). Wipe the SQLite file when the model changes.

**Dashboard (FastAPI + Jinja2 + HTMX + vanilla CSS)**  

Server-rendered with progressive enhancement.  

Key surfaces:

- `/` — modular widget dashboard (GridStack)
- `/events` — event log
- `/system` — system / poller health
- `/config/pipeline` — sources, events, rules, actions
- `/config/dashboard` — widget layout
- `/config/style` — theme / appearance
- `/config/users`, audit log, help

Widgets are modular partials refreshed independently via HTMX. Layout is shared for the install (not per-user in v0.1).

**Authentication &amp; Authorization**  

- Cookie-based session auth (`session_username`)
- Password hashing with bcrypt
- CSRF protection
- Optional `PARA_SCOPE_SECURE_COOKIES`
- In-memory login rate limiting
- Any authenticated user has full access (intentional; no role split planned)
- Secrets encrypted at rest with Fernet (`PARA_SCOPE_SECRET_KEY`)
- Audit log of authentication and configuration changes

### 7. Data Model (Conceptual → Actual)

Core entities present in `app/models.py`:

- `User`
- `Secret` (encrypted, scoped)
- `Source`
- `EventTypeRecord`
- `PollingSchedule`
- `Rule`
- `ActionInstance`
- `Event`
- `Field` + `FieldLogEntry`
- `AuditLog`
- `DashboardLayout`
- `AppSettings` (theme, font, background)
- `PushSubscription`

Relationships are straightforward; the design favors clarity over extreme normalization.

### 8. Extensibility Model

Today the system exposes simple in-process registration hooks:

- `register_poller(handler_type, fn)` in `app/pollers.py`
- `register_action(action_type, handler)` in `app/actions.py`

Extensions are built-in modules imported by Para-Scope and registered through these hooks. Dynamic plugin directories and Python entry-point discovery are intentionally out of scope.

New pollers only need to return a result dict (`ok`, `data`, optional `raw` / `response_time_ms`) and declare their config metadata. The rest of the pipeline (event creation, `on_success` / `on_failure` / optional `always`, rules, actions) remains identical.

### 9. Configuration Philosophy

- Runtime configuration and state live in the database.
- Environment variables (`.env`) handle bootstrap concerns (`PARA_SCOPE_SECRET_KEY`, secure cookies, VAPID keys, database URL, log level, uploads dir).
- Backups copy the SQLite database together with the matching secret key.
- No requirement for a complex configuration management system.

### 10. Deployment &amp; Operations (Pure Git Repository)

Primary distribution method: clone the git repository.

Expected developer / operator workflow:

- Create a Python virtual environment (`uv` recommended)
- Install dependencies from `requirements.txt`
- Copy `.env.example` → `.env` and set at least `PARA_SCOPE_SECRET_KEY`
- Start with `uvicorn app.main:app` (single worker)
- First visit goes to `/setup` to create the initial user (or use `create_user.py`)

Production path (documented in README):

- systemd unit running a single uvicorn worker bound to localhost
- nginx reverse proxy + Certbot for TLS
- `PARA_SCOPE_SECURE_COOKIES=1`

No Dockerfiles are required or encouraged as the primary path.

Backup strategy: copy the database file together with the matching secret key.

### 11. Security Considerations

- Webhook endpoints verify HMAC signatures when a secret is configured (timestamp bound into the MAC). Unsigned sources remain for local/dev.
- Secrets encrypted at rest (Fernet); decryption key is `PARA_SCOPE_SECRET_KEY`.
- Principle of least privilege for action credentials.
- Input validation and size limits on webhook payloads.
- Rate limiting on login (in-memory); webhook abuse protection recommended at the reverse proxy.
- Secure cookie flag opt-in via env; TLS termination left to the reverse proxy.
- Auditability of configuration and privileged actions.
- CSRF protection on state-changing forms.

### 12. Observability of the System Itself

The dashboard includes a system section and `/system` page showing poller job status, recent runs, success/failure counts, DB size, failed events, and related health. Self-metrics use existing tables and Field sinks — there is no separate metrics store or `/metrics` page.

Internal events can be treated like any other source so the same pipeline can alert on problems with the dashboard itself.

### 13. Open Questions and Future Extensions

- Whether condition language needs OR groups or length ops.
- Degree of multi-tenancy / workspace isolation (if any).
- Long-term data retention and cold storage.
- Official application distribution (PyPI package or standalone binary; not plugin loading).
- Phase-2 / future poll integrations: Docker/runtime snapshots, SMART disk health, ZFS/Btrfs pool health, MQTT broker checks, and deeper Redis/RQ/Celery queue integrations.
- ✅ Dashboard Triggers widget + `trigger_source` action (poll Run now or webhook ingest with templated payload; cascade depth 3).
- ✅ Never-run / trigger-only poll schedule.
- ✅ Maths helpers `trunc` / `sum` / `avg`; source templates; rule dry-run; `fields.<slug>` in actions and rules.
- ✅ Notify convenience action (`notify`: ntfy / Gotify / Discord) on top of shared HTTP forward.
- ✅ SSE live event tail; self-metrics on `/system`.
- Local script actions require `PARA_SCOPE_ALLOW_LOCAL_ACTIONS=1` (optional `PARA_SCOPE_LOCAL_ACTION_ALLOWLIST`).

### 14. Success Criteria for an Initial Usable Version (v0.1 status)

- ✅ A user can register webhook and poll sources.
- ✅ Events appear in the event log and can trigger `field_push`, `http_forward`, `notify`, `web_push`, and (when enabled) `local_script`.
- ✅ Basic graphs and modular widgets are available.
- ✅ Configuration is performed through the UI.
- ✅ The entire system runs from a git clone + virtualenv with minimal ceremony.
- ✅ Strong session authentication protects the dashboard.
- ✅ Adding a simple new action or poller is documented ([docs/authoring.md](docs/authoring.md)).