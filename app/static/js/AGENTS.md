# app/static/js/ — Browser scripts

## Role

Vanilla first-party scripts loaded by templates that need them. No module bundler — `<script>` order matters.

## File map

| File | Owns |
|------|------|
| `htmx.min.js` | Vendored HTMX (upgrade in place; treat as third-party) |
| `webpush.js` | Push toggle + SW register |
| `dialogs.js`, `disclosures.js` | Config UX |
| `source-dialog.js`, `conditions-builder.js` | Pipeline dialogs |
| `dashboard-grid.js` | GridStack layout save |
| `widget-charts.js`, `widget-clock.js`, `widget-notes.js`, `triggers.js` | Widget clients |
| `events.js` | Events page SSE live tail |

## Invariants

- `widget-*.js` pairs with `templates/widgets/{kind}_content.html` via `data-*` hooks.
- CSRF for JSON: read meta `csrf-token` / send `X-CSRF-Token` (same pattern as forms).
- Do not duplicate HTMX under `vendor/` without removing the `js/` copy.

## Prefer / avoid

- Prefer small feature files over a single mega-script.
- Prefer data attributes + event delegation over inline handlers in templates.
- New third-party libs → `vendor/`, not here (except upgrading existing htmx).

## See also

[static/AGENTS.md](../AGENTS.md) · [templates/widgets/AGENTS.md](../../templates/widgets/AGENTS.md)
