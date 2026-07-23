**Modular Event Dashboard – Design Document**  

*Working title: Para-Scope*

Version: 0.1  

Date: 2026-07-17  

Status: Living design. **AGENTS.md** is the ops contract for agents; this document is vision plus an explicit shipped-vs-planned split below.

### 0. Shipped in v0.1 vs Planned

**Shipped (matches the codebase today):**

- Single-process FastAPI + SQLite (`create_all` + `ensure_schema` patches; no Alembic)
- Cookie sessions, CSRF, Fernet-encrypted secrets, login rate limit
- Sources (webhook + poll), event types, multiple polling schedules, rules, actions (`field_push`, `http_forward`, `web_push`)
- Field sinks (logbook / counter / value / toggle) + MetricPoint + AuditLog + DashboardLayout widgets
- Webhook HMAC when a secret is configured (`X-Webhook-Timestamp` + HMAC over `{timestamp}.{body}`); unsigned sources allowed for local/dev
- In-memory login and webhook rate limits (single-process ceiling)
- In-process APScheduler pollers; HTMX dashboard refresh (not SSE)
- Action/poller registries in-process (not a plugin directory)

**Planned / not in v0.1 (do not treat as implemented):**

- PostgreSQL as a first-class backend, YAML/JSON config export-import, durable queue / dead-letter
- SSE live event tail, TOTP/WebAuthn/OIDC, API tokens
- Condition rate limits / time windows; formal plugin/adapter packaging and “Writing a Source Adapter” guides
- Mandatory webhook secrets in production (operator guidance only today)

---

### 1. Vision and Purpose

A self-hosted, single-process (or lightly multi-process) Python application that acts as a unified event hub and operational dashboard for personal or small-team infrastructure and projects.  

Users register **Sources** (Flit PKM, trading bots, e-commerce/payment providers, Uptime Kuma, custom services, etc.). Each source can emit events via verified webhooks and/or be actively polled on configurable schedules. Incoming events are normalized, filtered, and routed to one or more **Actions**. A modular web dashboard provides graphs, event logs, status overviews, and configuration surfaces.  

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
- Composable actions triggered by events (with conditions).
- Persistent event history + aggregated metrics suitable for graphs and audit logs.
- Attractive, modular, authenticated web dashboard built with FastAPI + Jinja2 + HTMX + vanilla CSS.
- Strong authentication and secret handling.
- Easy local development and deployment from a pure git repository.
- Clear extension points so new sources and actions can be added without core changes.
- Reasonable defaults and progressive complexity so a single user can start quickly while power users can go deep.

### 3. Non-Goals

- Multi-region high-availability or horizontal scaling as a primary concern (single-node / small-team first).
- Built-in long-term metrics storage that competes with dedicated time-series databases.
- Visual workflow designer or complex branching logic (keep the action model relatively linear + filters).
- Native mobile apps (responsive web is sufficient).
- Mandatory cloud dependencies or telemetry.
- Docker / container-first packaging (git + Python environment is the primary distribution method).

### 4. Core Domain Concepts

**Source**  

A registered origin of events. Examples: Flit, Verdant Prints / payment provider, alpaca trading bot, Uptime Kuma instance, generic HTTP endpoint, custom script.  

A source owns:

- Identity and metadata (name, description, tags, icon, base URL if relevant)
- Authentication / verification material for webhooks (secrets, optional signature schemes)
- Zero or more webhook event type definitions
- Zero or more independent polling schedules
- Associated secrets (API keys, tokens) used by pollers or actions
- Status (enabled/disabled, last seen, health)

**Event Type**  

A named kind of occurrence belonging to a source (e.g. `client.created`, `order.paid`, `position.opened`, `monitor.down`).  

Carries a schema hint or extraction rules so the system can pull useful fields into a normalized envelope.

**Event**  

A concrete occurrence. Normalized internal representation plus the original payload. Immutable once accepted.

**Polling Schedule**  

A named, independent job attached to a source: interval (or cron), target (URL, query, or custom handler), optional parameters, timeout, and retry policy. Multiple schedules per source are first-class.

**Action**  

A side-effect that can be attached to one or more event types (or to a source more broadly). Examples:  write to log store, update a counter/metric, forward HTTP webhook, run a short transformation.  

Actions support simple field-match conditions (exact, not, gt/lt, contains, regex). Rate limiting and time windows are planned, not shipped.

**Rule**  

The binding of event type(s) + optional conditions → one or more actions (with ordering or parallel execution semantics).

**Dashboard View / Widget**  

A modular visualization or control surface that consumes events, metrics, or source status. Layouts are user-configurable at a basic level.

### 5. High-Level Architecture

Single primary process (FastAPI application) that hosts:

- HTTP server (dashboard + webhook ingress endpoints)
- Background scheduler for polling jobs
- Event processing pipeline (receive → normalize → filter → execute actions → persist)
- Lightweight task mechanism for reliability (in-process `BackgroundTasks` today; durable queue planned)
- Auth middleware and session management
- Static file serving for CSS, HTMX, and any small client assets

Storage defaults to SQLite for zero-config simplicity. PostgreSQL is a planned option, not a supported path in v0.1.

Runtime configuration (sources, rules, schedules, dashboard layouts) lives in the database. Versionable YAML/JSON export/import is planned. The git repository contains the application code, templates, static assets, and documentation.

Data flow (simplified):

1. External system → Webhook POST (verified) **or** internal poller runs
2. Ingress / Poller produces a raw event
3. Normalization layer produces a canonical Event
4. Rule engine evaluates matching rules and conditions
5. Matching Actions are executed (immediate retries in-process; no dead-letter queue yet)
6. Event and any derived metrics are persisted
7. Dashboard and HTMX-polled feeds consume the stores

### 6. Detailed Component Responsibilities

**Source Registry &amp; Management**  

CRUD for sources, event type registration, secret association, enable/disable, health tracking. Provides the admin UI surface and the programmatic API used by the rest of the system.

**Webhook Ingress**  

Per-source (or shared with source discrimination) HTTPS endpoints.  

Responsibilities:

- Signature / secret verification (support common schemes: HMAC-SHA256, etc., and allow custom verifiers)
- Replay protection / basic rate limiting
- Event type extraction and validation against registered types
- Immediate acknowledgment (202) while processing continues asynchronously where possible
- Clear error responses and logging for misconfigured senders

**Polling Engine**  

A scheduler (APScheduler-style or equivalent) that runs independent jobs.  

Each schedule knows its source, interval/cron, handler, and parameters.  

Handlers can be:

- Generic HTTP GET/POST with JSON extraction
- Source-specific Python callables registered via the extension mechanism
- Simple script execution (carefully sandboxed or restricted)

Jobs report success/failure, latency, and extracted events back into the same pipeline as webhooks. Jitter, concurrency limits, and per-source backoff are important.

**Event Pipeline**  

- Acceptance and normalization
- Optional enrichment
- Rule matching and condition evaluation
- Action dispatch (parallel where safe, ordered when declared)
- Persistence of the event and side-effect outcomes
- Emission of internal system events (for the dashboard’s own observability)

**Action Engine**  

Registry of action types. Each action receives the normalized event + its own configuration.  

Built-in actions should cover notifications (webPush and api based), metric updates, logging, and HTTP egress.  

Custom actions are added via the extension points.  

Actions are expected to be idempotent where practical and to report structured results.

**Storage Layer**  

Two main concerns:

- Event log (append-only, query-able by time, source, type, correlation ID, full-text on key fields)
- Metrics / time-series (counters, gauges, simple histograms or pre-aggregated buckets for graphs)

Retention policies, down-sampling, and optional archival are configurable. The design should allow swapping the backend later without rewriting the whole application.

**Dashboard (FastAPI + Jinja2 + HTMX + vanilla CSS)**  

Server-rendered with progressive enhancement.  

Key surfaces:

- Overview / status board (sources health, recent critical events)
- Per-source detail pages
- Global and filtered event log with search (HTMX partials; SSE live tail planned)
- Graphing views (time-range selectors, multiple series)
- Configuration UI for sources, schedules, rules, and actions
- System health and audit views

Widgets are modular: a page is composed of reusable partials that can be refreshed independently via HTMX. Layout preferences are stored per user.

**Authentication &amp; Authorization**  

- Strong session-based auth for the dashboard (password today; optional TOTP/WebAuthn or OIDC planned)
- Single user type: any authenticated user has full access (no admin/viewer roles)
- Per-source webhook secrets and action credentials stored encrypted at rest
- API tokens for programmatic access (planned)
- Audit log of authentication events and configuration changes
- CSRF protection, optional secure cookies (`PARA_SCOPE_SECURE_COOKIES`), rate limiting on login and webhooks (in-memory, single-process)

### 7. Data Model (Conceptual)

Core entities (high-level):

- User (for dashboard auth)
- Source (id, name, slug, type/adapter, config, secrets references, enabled, timestamps)
- EventType (belongs to Source, name, description, schema/extraction hints)
- PollingSchedule (belongs to Source, name, cron/interval, handler, params, enabled)
- Rule (name, event type filters or source-wide, conditions, list of Action bindings, enabled)
- ActionInstance (type, configuration, secrets references, enabled)
- Field (global sink: logbook / counter / value / toggle — used by `field_push` and widgets)
- Event (id, source_id, event_type, timestamp, normalized fields, raw payload, correlation_id, processing status)
- MetricPoint or aggregated series (source, field, name, timestamp, value, tags)
- AuditLog / SystemEvent
- DashboardLayout (shared install widget layout) + AppSettings (global theme)

Relationships are straightforward; the design favors clarity over extreme normalization.

### 8. Extensibility Model

The system exposes in-process registration hooks (`register_action` / `register_poller`) today. A richer plugin directory or entry-point packaging is planned for:

- Source adapters (help with default event types, specialized pollers, verification schemes)
- Action types
- Custom extraction / normalization logic
- Dashboard widgets or visualization helpers

A new action or poller is addable by registering a callable in the appropriate module. Core stays unaware of specific integrations (Flit, trading bots, Stripe-like providers, etc.).

### 9. Configuration Philosophy

- Runtime configuration and state live in the database.
- Example configs and full YAML/JSON export/import are planned for GitOps-style workflows.
- Environment variables (`.env`) handle bootstrap concerns (database URL, secret key, secure cookies, VAPID, etc.).
- No requirement for a complex configuration management system.

### 10. Deployment &amp; Operations (Pure Git Repository)

Primary distribution method: clone the git repository.

Expected developer / operator workflow:

- Create a Python virtual environment (uv, poetry, or plain venv + pip)
- Install dependencies from a lockfile or requirements
- Copy and edit a small example configuration / .env
- Run database migrations / initialization
- Start the application with uvicorn (or an equivalent ASGI server) under a process supervisor if desired (systemd, supervisord, etc.)
- Place a reverse proxy (Nginx, Caddy, etc.) in front for TLS and additional hardening

The repository contains:

- Application source
- Jinja templates and static assets (vanilla CSS, vendored HTMX/Chart.js/GridStack)
- Schema evolution via `create_all` + `ensure_schema` (no migration scripts yet)
- Documentation (this design doc + AGENTS.md + README)
- Simple scripts for common tasks (`create_user.py`; config export planned)

No Dockerfiles or compose files are required or encouraged as the primary path, although community contributions of that nature can live in a separate folder or repository.

Backup strategy: database file (or logical dump) + exported configuration YAML. Secrets should be handled carefully (encrypted or external secret store).

### 11. Security Considerations

- Webhook endpoints verify HMAC signatures when a secret is configured (timestamp bound into the MAC). Unsigned sources remain for local/dev.
- Secrets encrypted at rest (Fernet); decryption key is `PARA_SCOPE_SECRET_KEY`.
- Principle of least privilege for action credentials.
- Input validation and size limits on webhook payloads.
- Rate limiting on login; webhook abuse protection is recommended at the reverse proxy and may be added in-process later.
- Secure cookie flag opt-in via env; TLS termination left to the reverse proxy.
- Auditability of configuration and privileged actions.

### 12. Observability of the System Itself

The dashboard includes a system section showing:

- Poller job status and recent runs
- Webhook acceptance rates and errors
- Action success/failure counts
- Queue depths or processing lag
- Basic resource usage if easily available

Internal events are treated like any other source so the same pipeline can alert on problems with the dashboard itself.

### 13. Open Questions and Future Extensions

- Exact metrics storage approach (pure SQLite tables vs embedded time-series vs external).
- How sophisticated the condition language needs to be on day one.
- Degree of multi-tenancy / workspace isolation required for broader adoption.
- Whether to support outbound webhook signing and delivery guarantees as a first-class action.
- Long-term data retention and cold storage story.
- Official packaging (PyPI application entry point, standalone binary via PyInstaller/Nuitka, etc.).
- Naming and branding.

### 14. Success Criteria for an Initial Usable Version

- A user can register at least two different sources (one webhook-heavy, one polling-heavy).
- Events appear in a live log and can trigger at least notifications + metric updates.
- Basic graphs show trends over 24h / 7d.
- Configuration can be performed through the UI (YAML export planned).
- The entire system runs from a git clone + virtualenv with minimal ceremony.
- Strong authentication protects the dashboard.
- Adding a simple new action or source adapter is documented and reasonably straightforward.
