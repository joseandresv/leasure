# design-sync notes — Leasure

- This repo is a Python (FastAPI + Jinja2 + htmx) app, NOT a JS component library.
  There is no package.json, no dist/, no Storybook. The standard converter cannot run.
- Scope agreed with user (2026-07-10): **tokens-only sync** — design tokens extracted
  from static/css/theme.css (:root block), Google Fonts bundled as woff2, style-guide
  previews and guidelines authored by hand (off-script layout).
- No components are uploaded, so no _ds_bundle.js and no _ds_sync.json anchor
  (honest omission per the skill — a future sync re-verifies everything).
- Token source of truth: static/css/theme.css lines ~11-104. If the theme changes,
  re-extract the :root block into ds-bundle/tokens/tokens.css and re-upload.
- Fonts: Inter (400-700), JetBrains Mono (400,500,700), Orbitron (500-900),
  Press Start 2P — fetched from Google Fonts as latin woff2 subsets.
