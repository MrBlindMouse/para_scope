# app/static/ — Static assets

## Role

Mounted at `/static` from `app/main.py`. First-party CSS/JS plus vendored libs. `sw.js` is also served at `/sw.js` (webhook router) for Web Push — special-cased, not only under `/static`.

## File map

| Path | Owns |
|------|------|
| `favicon.svg` | Site icon |
| `sw.js` | Service worker (push) |
| `css/` | First-party STYLE.md layers — see [css/AGENTS.md](css/AGENTS.md) |
| `js/` | App scripts (+ vendored htmx in place) — see [js/AGENTS.md](js/AGENTS.md) |
| `vendor/` | Third-party — see [vendor/AGENTS.md](vendor/AGENTS.md) |

## Invariants

- No build step / bundler. Templates load scripts and CSS explicitly.
- Static paths are CSRF/auth exempt via webctx middleware prefixes.
- Prefer putting new third-party libs under `vendor/`, not `js/` (htmx historically lives in `js/` — upgrade in place; do not invent a second copy).

## See also

[css/](css/AGENTS.md) · [js/](js/AGENTS.md) · [vendor/](vendor/AGENTS.md) · [app/AGENTS.md](../AGENTS.md)
