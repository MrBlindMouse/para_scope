# Changelog

All notable changes to Para-Scope are documented here.

The install unit is **(git tag + `para_scope.db` + `PARA_SCOPE_SECRET_KEY`)**. Schema is models + `create_all` only — there are no migrations. Breaking releases say what to wipe.

Versioning: [Semantic Versioning](https://semver.org/) on the `0.x` line. App version string: `app/main.py` (`FastAPI(..., version=...)`). Releases are **git tags** on `main` (`v0.1.0`, …). No long-lived version branches unless a backport is required.

While still on `0.x`, a **MINOR** bump may include breaking (wipe) changes; those are called out under **Breaking**. After `1.0.0`, breaks move to **MAJOR** only.

## [0.1.0] — 2026-07-31

Initial usable release. Tag: `v0.1.0`.

### Added

- Webhook and poll sources, event types, rules, and built-in actions (`field_push`, `http_forward`, `notify`, `web_push`, `local_script`, `trigger_source`)
- Shared Fields, dashboard widgets (GridStack + ApexCharts), themes / style config
- Dashboard Triggers widget; never-run (trigger-only) poll schedules; nested trigger cascade depth 3
- SSE live event tail on `/events`; self-metrics on `/system`
- Source templates (quick-add), rule dry-run, maths helpers, bare-slug widget text templates
- Cookie sessions, CSRF, Fernet secrets, systemd + nginx ops docs, [docs/authoring.md](docs/authoring.md)

### Breaking

- Baseline release: start from an empty DB (or wipe `para_scope.db` / `.test_db.sqlite` including `-wal`/`-shm` if upgrading from pre-tag development).
- `Rule.source_id` is required (no global rules).

### Security / reliability

- Fail-fast if `PARA_SCOPE_SECRET_KEY` is empty
- Outbound HMAC signs the same JSON bytes that are sent
- Discord webhook timestamp skew; GitHub replay ceiling documented
- Forward-only pipeline deletes (source → event type → rule → action)
- Poll disable removes jobs (no spurious backoff); pipeline stops on first action failure
