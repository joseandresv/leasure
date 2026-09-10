---
name: jinja-htmx
description: Jinja2 template and htmx/Alpine peculiarities in Leasure — autoescape and safe output, macros and partial contracts, tojson in attributes, htmx swap/trigger semantics and lifecycle events, Alpine stores, panel structure (masthead / deck / menu-foot). Load before editing anything under templates/.
---

# Jinja2 + htmx + Alpine in Leasure

The UI is server-rendered partials swapped by htmx into a single-page shell
(`templates/index.html`), with small Alpine.js stores (toasts) and vanilla JS
(`static/js/*.js`). There is no build step: what is in `templates/` and `static/` is
what the browser gets, and both are served fresh (no server restart for template/CSS
changes; Python changes do need one).

## 1. Escaping

- `Jinja2Templates` autoescapes `.html`. Every `{{ value }}` is HTML-escaped, so
  provider data (titles, artists, URLs, genres) is safe by default. Never add `|safe`
  to anything that could carry user or provider text.
- Passing an HTML entity string into a macro (`ctl('&#9000;', ...)`) prints it literally
  because of autoescape. Pass the actual Unicode glyph (`⌨`) instead.
- Values that must reach inline JS go through `{{ value | tojson }}` **inside a
  single-quoted attribute** (`onclick='selectDrive({{ d.path | tojson }})'`): `tojson`
  escapes `<`, `>`, `&`, `'` but not `"`, and Windows paths contain backslashes that
  break naive quoting.
- URLs used as `href`/`src` must already be http(s); the server filters them, the
  template should not build one from pieces.

## 2. Macros and the panel skeleton

- `templates/macros/components.html` defines `menu_head(kicker, title)`, `ctl(key,
  label)`, `stage(name)` and friends. Every panel under `templates/panels/` follows:
  `menu_head` → `.menu-deck` (stage column + `.menu-content`) → `.menu-foot` of `ctl()`
  hints. Keep that order; CSS (`css-craft` skill) depends on it: the masthead is
  pinned, the deck is the only scroller.
- A macro used in a file must be imported at the top of that file
  (`{% from "macros/components.html" import ctl %}`); imports are not inherited.
- Partials under `templates/partials/` are the contract between routers and the page:
  their root element ids and the htmx targets in the panels must match
  (`#device-list`, `#sync-diff`, `#device-files`, `#yt-setup-result`, `#music-results`).
  Renaming an id means updating every `hx-target` and JS `getElementById` that uses it.
- Templates receive globals `platform` and `device_path_placeholder`; anything else
  must be passed explicitly in the router's `context`.

## 3. htmx semantics that bite

- `hx-get` + `hx-trigger="load"` fires when the element is *processed*, which for this
  SPA is at boot for every panel (all five are in the DOM), not when the panel becomes
  visible. Polling (`every 5s`) also runs for hidden panels. Prefer `hx-trigger="revealed"`
  or a custom event fired by `scene.js` when the panel activates, if you add new loads.
- `hx-swap="innerHTML"` replaces children; `outerHTML` replaces the element itself —
  after `outerHTML` the old `id` must be present in the new markup or the next request
  has no target.
- Inline `<script>` inside a swapped partial runs on swap (htmx evaluates it). That is
  how `device_list.html` sets `window.leasureDrives`; keep such scripts tiny and
  idempotent because every re-scan re-runs them.
- Listeners added on `htmx:afterSwap`/`htmx:load` must be guarded against double
  binding (`dataset.bound`); `rhythm-deck.js` had this bug.
- Form posts: `hx-post` sends `application/x-www-form-urlencoded`; FastAPI handlers use
  `Form(...)`. `hx-include="#id"` pulls extra inputs. Cross-site requests are rejected by
  the server; same-origin htmx requests carry the right headers automatically.
- Non-2xx responses are **not swapped** by default; an error partial must be returned
  with status 200 if it should render, or the JS must handle `htmx:responseError`.
- Errors that the page renders inline come back as small HTML fragments, not JSON;
  JSON endpoints are for `fetch()`/JS callers only. Never point an `hx-get` at a JSON
  endpoint (the raw-JSON footer bug).

## 4. Alpine

- Alpine is loaded from CDN with `defer`; stores are defined in `index.html`
  (`Alpine.store('toast')`). Access them after Alpine initialises
  (`document.addEventListener('alpine:init', ...)` or lazily inside handlers).
- `x-data` scopes state to a subtree; a panel partial swapped by htmx inside an
  `x-data` element is re-initialised by Alpine's mutation observer — avoid heavy
  `x-init` work in partials.

## 5. Copy inside templates

- Follow the `frontend-design` skill for copy: plain language, active voice, errors say
  what happened and what to do. Configuration internals (env var names, file paths)
  belong inside a collapsed `<details><summary>How to set up</summary>`.
- Consistency across the connection cards: same lead sentence pattern, same summary
  label, same open/closed default.

## 6. Verify

- `pytest -q`: the API tests render the partials, so a Jinja syntax error fails there.
- `curl` the partial's URL and read the markup; then the `ui-testing` probe for the
  rendered result and console errors.
