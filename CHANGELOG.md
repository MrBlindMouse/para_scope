# Changelog

All notable changes to Para-Scope are documented here.

The install unit is **(git tag + `para_scope.db` + `PARA_SCOPE_SECRET_KEY`)**. Schema is models + `create_all` only — there are no migrations. Breaking releases say what to wipe.

Versioning: [Semantic Versioning](https://semver.org/) on the `0.x` line. App version string: `app/main.py` (`FastAPI(..., version=...)`). Releases are **git tags** on `main` (`v0.1.0`, …). No long-lived version branches unless a backport is required.

While still on `0.x`, a **MINOR** bump may include breaking (wipe) changes; those are called out under **Breaking**. After `1.0.0`, breaks move to **MAJOR** only.

## [Unreleased]

### Breaking

- JSON shapes in Update field (data **Object from event**, logbook **Value from event**): quoted string leaves are now **literal text**. Paths and maths must use `{{ … }}` (e.g. `{"celsius":"{{ temp }}"}` instead of `{"celsius":"temp"}`). No DB wipe — reconfigure affected action `value_key` shapes. Bare (non-JSON) paths like `payload` are unchanged.

### Changed

- Help, action-form tips/placeholders, and DESIGN align with template-style shape leaves

## [0.1.1] — 2026-07-31

Patch release. Tag: `v0.1.1`. No schema wipe.

### Fixed

- Multi-series graphs use a datetime x-axis (`{x,y}` points) instead of category null-padding, so staggered series no longer interfere
- Visual rule conditions coerce `true`/`false`/`on`/`off`/`yes`/`no` so toggle Fields match via `fields.<slug>.value`
- Widget tone rules coerce the same bool literals (toggle backgrounds / kv rules)
- Data-field series with no real timestamps under Hours range surfaces an error (use Entries) instead of a blank chart
- Empty series payloads no longer mount an empty Apex shell
- Chart sources with a missing numeric extract are skipped (no fake `0.0` slices)
- Table widget keeps multiple paths on the same Field; toggle string `"false"`/`"off"`/`"0"` reads as off
- Series column `horizontal` uses proper bool config parsing

### Changed

- Config UI: source-count hints for multi/stacked/radar/polar styles; table Background control removed; toggle/board-toggle Background labeled “From on/off”
- Help / conditions tip mention toggle Field paths and bool values
- Maths (`eval_expr`) comparisons `=` `!=` `<` `>` `<=` `>=` (bare `=` means equal) return `1` or `0`; string/bool for equality ops, order ops need numbers
- Toggle and board-toggle Field input accepts a compare/maths expression (On when result ≠ 0) as well as a classic toggle Field slug

### Added

- Node pack check for multi-series datetime packing (`widget-charts-pack-check.js` + pytest)
- Dashboard / Help copy for toggle Field expressions (e.g. `myservice.status = ok`, `load.value > 80`)

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

### Fixed

- World-clock **Add timezone** no longer a no-op (editor normalize was dropping empty draft rows).

### Security / reliability

- Fail-fast if `PARA_SCOPE_SECRET_KEY` is empty
- Outbound HMAC signs the same JSON bytes that are sent
- Discord webhook timestamp skew; GitHub replay ceiling documented
- Forward-only pipeline deletes (source → event type → rule → action)
- Poll disable removes jobs (no spurious backoff); pipeline stops on first action failure
