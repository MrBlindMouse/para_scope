# Para-Scope Feature Inventory

## Newly proposed context features

- `dt` **standard variable:** structured date/time context using the configured display timezone plus UTC. Pipelines anchor it to the persisted event timestamp for deterministic retries; widgets use render time. Intended members should include ISO text, epoch, date/time parts, and a UTC sub-object. The name is already in `RESERVED_FIELD_SLUGS`.
- `system` **standard variable:** app-only metadata, available to widgets and pipeline actions—Para-Scope name/version and configured timezone, with no hostname, OS, memory, disk, load, or other host disclosure. The name is already in `RESERVED_FIELD_SLUGS`.
- The shared safe evaluator in `[app/widget_transforms.py](/home/johan/programming/para-scope/app/widget_transforms.py)` remains the single expression engine. No new parser or dependency is warranted.



## Explicitly planned and not shipped

The authoritative split is already listed in `[DESIGN.md](/home/johan/programming/para-scope/DESIGN.md#L26-L41)`.

### Pipeline and dashboard capability

- Heatmap, calendar heatmap, and range-column widgets — still deferred.
- Markdown notes display — still deferred.

### Reliability and operations

- Mandatory webhook secrets in production — still deferred.

### Extensions and platform support

- Phase-2 pollers: Docker/runtime, SMART, ZFS/Btrfs, MQTT, and deeper queue integrations — deferred.
- Official PyPI/binary packaging remains an open extension rather than a commitment.

## Shipped (was planned)

- Cross-Field access: `fields.<slug>.<path>` in actions and rule conditions; reserved `field` for Update Field target.
- Unified dashboard slug namespace: `{{ slug.value }}` in templates, tone rules, Triggers payloads, and widget titles/labels/units/link URLs (not `fields.*`; notes stay literal).
- Reserved Field slugs enforced via `RESERVED_FIELD_SLUGS` in `app/fields.py`.
- Maths helpers: `trunc`, `sum`, `avg` (variadic scalars).
- Rule test / dry-run.
- Source templates / quick-add (full stack: source + fields + rules + widgets).
- Authenticated dashboard Triggers widget + `trigger_source` action; never-run poll schedule; cascade depth 3.
- SSE live event tail on `/events`.
- Self-metrics on `/system` (DB size, failed events, schedule overdue).
- Authoring guides: [docs/authoring.md](docs/authoring.md).
- Forward-only pipeline delete cascade (source → event type → rule → action).

## Deliberately out of scope

- PostgreSQL support and migrations. Para-Scope remains SQLite-only with `create_all` and explicit wipe/recreate schema changes.
- Authentication expansion: TOTP, WebAuthn, OIDC, API tokens, and admin/viewer roles. Keep the existing password, signed-cookie session, and all-authenticated-users model.
- Stateful condition rate limits and time windows. They require persistent counters, reset/window semantics, and concurrency rules that would make the pipeline substantially heavier.
- YAML/JSON config import/export and related portability UI are not currently required. Backups remain the SQLite database plus the matching secret key.
- Durable queues, dead-letter processing, and delivery guarantees. Actions remain immediate and in-process; Para-Scope is not a workflow automation engine.
- Plugin discovery or Python entry-point loading. Extensions remain built-in modules using the existing `register_action` and `register_poller` registries.



## Remaining partial implementation

- **Event-log query claim is partial:** source/type/status filters exist; time and correlation-ID filtering do not.

## Maintenance debt worth tracking separately

- Add handler tests for the six registered but untested pollers. This is test-coverage debt, not a missing product feature.



## Suggested consideration order

1. Add the requested `dt` and `system` context contract as one small shared capability (names already reserved).
2. Evaluate heatmap / markdown-notes widgets if dashboard density becomes valuable.
3. Keep delivery and extension loading deliberately simple (no plugin systems, no durable queues).
