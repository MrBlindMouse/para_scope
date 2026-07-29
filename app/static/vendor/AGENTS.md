# app/static/vendor/ — Third-party assets

## Role

Vendored libraries. Do not edit except when upgrading.

## Contents

- `apexcharts/` — charts (series/chart widgets)
- `gridstack/` — dashboard grid layout

## Prefer / avoid

- Upgrade by replacing files in place; keep versions consistent with template `<script>` / `<link>` paths.
- Do not patch vendor source for app behavior — wrap in first-party JS/CSS instead.
- Leaf package dirs do not need their own AGENTS.md.

## See also

[static/AGENTS.md](../AGENTS.md)
