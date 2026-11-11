# Changelog

All notable changes to Para-Scope are documented here.

The install unit is **(git tag + `para_scope.db` + `PARA_SCOPE_SECRET_KEY`)**. Schema is models + `create_all` only — there are no migrations. Breaking releases say what to wipe.

Versioning: [Semantic Versioning](https://semver.org/) on the `0.x` line. App version string: `app/main.py` (`FastAPI(..., version=...)`). Releases are **git tags** on `main` (`v0.1.0`, …). No long-lived version branches unless a backport is required.

While still on `0.x`, a **MINOR** bump may include breaking (wipe) changes; those are called out under **Breaking**. After `1.0.0`, breaks move to **MAJOR** only.

## [Unreleased]

### Breaking

- `DashboardLayout` gains `name`, `slug`, and `kind` (`dashboard`, `calendar`, `kanban`), plus `calendar_items`, `kanban_columns`, and `kanban_cards`. **Wipe** `para_scope.db` and `app/.test_db.sqlite` (and `-wal`/`-shm`) before restart — layout and all other config/data are not migrated. If you already wiped for named pages, wipe again.

### Added

- Named dashboard pages: create/rename/delete under `/config/dashboard`; live view via `/?page={slug}`; header links when pages exist
- Page kinds: a page can be a dashboard, a week calendar, or a kanban board
- Read-only ICS poller (`ics_calendar`). Changes emit `item.upsert`, `item.removed`, and `focus.changed`
- `kanban_push` action and a **Calendar → kanban** pipeline template. Cards land in Next; moving a card emits `card.moved` on a reserved Board source

## [0.1.2] — 2026-08-18

Patch release. Tag: `v0.1.2`. No schema wipe.

### Breaking

- JSON shapes in Update field (data **Object from event**, logbook **Value from event**): keys must be quoted (may use `{{ }}`). **Bare** values are path/maths (`{"cash": cash}`); **quoted** values are literal text or templates (`"sensor"`, `"Sensor {{ name }}"`). Explicit `"{{ cash }}"` still works. No DB wipe — reconfigure shapes that relied on quoted path strings like `"temp"` as lookups (use bare `temp` or `"{{ temp }}"`). Bare (non-object) paths like `payload` are unchanged.

### Fixed

- Notes widget no longer loses edits: HTMX no longer polls notes; pending text flushes on blur/pagehide; `autocomplete="off"` so refresh cannot restore stale form values
- Dashboard and widget HTML is uncached (`Cache-Control: no-store`); HTMX GETs to `/widgets/` send a cache-buster and skip history snapshots
- Radial/chart widgets fill their card; series/chart/radial maths eval
- Widget preview alignment; toggle widget labelling
- JSON field updates treat quoted values as literals

### Added

- Maths `list` step (`math(list.**.value)`)
- Logbook fields usable on toggle widgets

### Changed

- Help, action-form tips/placeholders, and DESIGN document bare-value JSON-like shapes
- Auth/header chrome; dashboard layout fits narrower device screens

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
