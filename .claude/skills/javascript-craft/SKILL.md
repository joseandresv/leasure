---
name: javascript-craft
description: JavaScript peculiarities in Leasure's no-build frontend — vanilla ES2017+ in static/js, htmx lifecycle, Alpine stores, Three.js/CSS3D carousel in scene.js, EventSource sync stream, DOM safety (textContent/createElement), listener hygiene, WebGL and reduced-motion fallbacks. Load before editing static/js.
---

# JavaScript craft for Leasure

Four files, no bundler, no transpiler: `static/js/scene.js` (3D carousel of panels,
WebGL Walker device, panel activation), `leasure.js` (sync UI, drive selection, genre
graph, colour extraction), `rhythm-deck.js` (Music deck wheel), `app.js` (legacy). CDN
globals: `THREE` (r128 + CSS3DRenderer), `htmx`, `Alpine`, `Vibrant`, `graphology`,
`graphologyLibrary`, `Sigma`. Everything runs as classic scripts, so top-level names
are globals — prefix or wrap in an IIFE to avoid collisions.

## 1. Language level and style

- Target: evergreen Chromium/Firefox/Edge. `const`/`let`, arrow functions, template
  literals, optional chaining, `async/await`, `replaceChildren`, `inert` are all fine.
  No modules (`import`) unless the script tag becomes `type="module"` — which changes
  load order and scoping for everything that depends on globals.
- There is no linter for JS in CI. Read your diff twice; run the page in the
  `ui-testing` probe and check `console_errors`/`page_errors` in report.json.
- Keep functions small and named for what they do; no dead code, no commented-out
  blocks, comments only for a non-obvious *why*.

## 2. DOM safety

- Provider data (titles, artists, genres, URLs, error text) never goes into
  `innerHTML`. Build nodes with `document.createElement` and set `textContent`;
  set attributes with `el.setAttribute`/`el.style.setProperty`, and validate values
  that reach CSS (`/^#[0-9a-f]{3,8}$/i` for colours) or URLs (`^https?:`).
- `innerHTML` is acceptable only for a static literal with no interpolation.
- Prefer `replaceChildren()` to clear; `hidden` attribute to toggle visibility.

## 3. htmx and Alpine lifecycle

- Content arrives asynchronously via htmx swaps. Code that needs elements from a
  partial must run on `htmx:afterSwap`/`htmx:load` scoped to `event.detail.target`, or
  lazily inside the handler that needs them — never at script load.
- Bind listeners once: guard with `el.dataset.bound = '1'` or attach to a persistent
  ancestor with event delegation. Swaps replace children, so listeners on swapped
  nodes vanish while listeners on persistent nodes accumulate if re-added.
- `htmx.ajax('GET', url, target)` returns a promise; the target must exist.
- `Alpine.store('toast').add(text, kind)` for notifications; always guard with
  `if (window.Alpine && Alpine.store('toast'))`.

## 4. scene.js peculiarities

- Panels are cloned into `.carousel-panel` elements positioned on a CSS3D ring; sizes
  and offsets come from CSS variables (`--panel-w`, `--panel-h`) — read them with
  `getComputedStyle`, do not hard-code pixel constants.
- Exactly one panel is active: `navigateToPanel` toggles visibility and `inert` on the
  others. New per-panel behaviour belongs in that loop.
- The WebGL Walker (`#device-canvas`) is a fixed overlay above the panels; `placeDevice`
  anchors it to the active panel's `.device-dock`, `trackPanelBodyScroll` re-anchors on
  the panel body's scroll (rAF-throttled), `clipToScroller` clips it to the scroller.
  Fixed elements are not clipped by ancestors' `overflow`.
- Guard every WebGL entry point: constructing `THREE.WebGLRenderer` can throw (no GPU,
  headless, remote desktop). The UI must boot without it and not mark the stage as
  device-mounted when there is no renderer.
- `setTimeout` callbacks receive a lateness argument in Firefox: never pass a function
  with optional params directly (`setTimeout(() => fn(), ms)`).
- Respect `matchMedia('(prefers-reduced-motion: reduce)')`: skip the render loop and
  spring animations when it matches.

## 5. Network from JS

- Mutations use `fetch(url, { method: 'POST', body: new URLSearchParams(...) })`; read
  `resp.ok` and the JSON `detail` for errors and show them inline via the existing
  error helpers. A 4xx is expected for validation and must render a message, not a
  console-only failure — validate client-side first when the data is available
  (`window.leasureDrives`).
- Progress streams use `EventSource` on a job-id URL created by a prior POST; handle
  `start`/`progress`/`playlists`/`done`/`error` events, close on `done`/`error`, and on
  `onerror` tell the user the server-side job continues.
- Never build URLs from untrusted strings without `encodeURIComponent`.

## 6. Verify

- `ui-testing` probe with a custom steps script: exercise the interaction, screenshot,
  `page.evaluate` the DOM state you changed, and assert no console/page errors.
- For keyboard behaviour, drive `page.keyboard.press('Tab')` and record
  `document.activeElement`.
