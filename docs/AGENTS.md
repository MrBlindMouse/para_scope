# docs/ — Documentation assets

## Role

Non-code assets for README, marketing, and contributor guides. Not application runtime.

## Contents

- `dashboard.png` — live dashboard screenshot used in [README.md](../README.md)
- `authoring.md` — how to add a poller, action, or widget kind (built-in registries only)

Release notes and SemVer policy live at the repo root: [CHANGELOG.md](../CHANGELOG.md) (not under `docs/`).

## Prefer / avoid

- Keep screenshots here (or similarly under `docs/`), not in the repo root.
- Do not put runtime uploads here — that is `data/`.
- Prefer short recipes that point at existing registries over inventing frameworks.
- When cutting a release, update root CHANGELOG + version in `app/main.py`; do not invent a second version file under `docs/`.

## See also

Root [AGENTS.md](../AGENTS.md) · [README.md](../README.md) · [CHANGELOG.md](../CHANGELOG.md) · [authoring.md](authoring.md)
