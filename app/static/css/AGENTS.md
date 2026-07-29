# app/static/css/ — Vanilla CSS

## Role

First-party styles per [STYLE.md](../../../STYLE.md). No Sass/build. Entry is `main.css` with `@layer` cascade and `@import`s.

## File map

Layers in `main.css`: `reset → base → layout → components → utilities`.

| Layer | Files |
|-------|--------|
| reset | `reset.css` |
| base | `base.css`, `themes.css` |
| layout | `layout.css` |
| components | `buttons`, `inputs`, `cards`, `tables`, `feedback`, `config-nav`, `dialogs`, `pipeline`, `tooltips`, `help`, `conditions-builder`, `dashboard` |
| utilities | `utilities.css` |

## Invariants

- One concept per file; BEM (`.card`, `.card__header`, `.btn--primary`).
- OKLCH tokens in `themes.css` (`--lch-gray-*`, `--color-link`, …); dark via `prefers-color-scheme`.
- Component APIs = custom properties (e.g. `--btn-background`); variants = one-line overrides.
- New styles: pick a layer, add/extend a concept file, `@import` from `main.css`.

## Prefer / avoid

- Prefer utilities (`.flex`, `.stack`, `.mt`) over one-off classes.
- Widget/dashboard visuals → `dashboard.css` unless a new concept warrants its own file.
- Avoid inline `style=` in templates except for truly dynamic values.

## See also

[STYLE.md](../../../STYLE.md) · [static/AGENTS.md](../AGENTS.md)
