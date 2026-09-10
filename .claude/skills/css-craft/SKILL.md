---
name: css-craft
description: CSS mechanics and browser peculiarities for Leasure's frontend — sizing units and device scaling, fluid type, flex/grid overflow traps, sticky/scroll containers, masks and clipping, responsive breakpoints, cascade and specificity. Load with frontend-design before touching static/css or templates.
---

# CSS craft: the mechanics behind the look

The `frontend-design` skill (Anthropic) covers *what* the UI should feel like. This one
covers *why CSS does what it does* in this codebase, so fixes address causes, not symptoms.
Verify every claim below against the current `static/css/theme.css` before relying on it.

## 1. Units, scaling and why "big fonts" happen

- **CSS px are not device px.** Windows display scaling (125 %/150 %) and browser zoom
  change `devicePixelRatio`; a 1000-device-px window at 125 % is an 800-CSS-px viewport.
  Always reason in CSS px: `window.innerWidth`, not the monitor.
- **`rem` = root font size (16px unless `html { font-size }` is set).** Leasure sets no
  root size, so `1rem = 16px` everywhere and nothing scales with the viewport by itself.
- **`em` = the element's own font size.** Use `em` for things that must scale with their
  text (inline `code`, icon glyphs, padding inside buttons); use `rem` for layout rhythm.
  Inline `code` sized in `rem` next to `1rem` body text is the classic "tiny monospace"
  mismatch — set `code { font-size: 0.9em }` instead.
- **Fluid type**: `font-size: clamp(0.85rem, 0.7rem + 0.5vw, 1rem)`. The middle term is
  what makes it fluid; the min/max keep it readable. Never use bare `vw` for text.
- **Line length** should stay under ~80 characters (`max-width: 65ch` on prose).
- **`text-size-adjust`**: mobile browsers inflate text in wide blocks; set
  `-webkit-text-size-adjust: 100%` on `html` if it ever bites.

## 2. Overflow traps in flex and grid

- A flex/grid child's `min-width`/`min-height` defaults to `auto` = its content size, so
  it refuses to shrink and overflows the container. Fix with `min-width: 0` /
  `min-height: 0` on the child that should scroll or wrap.
- `overflow-y: auto` only scrolls if the element has a bounded height. In a flex column
  that means `flex: 1 1 auto; min-height: 0` on the scroller and a fixed-height parent.
- Long unbreakable strings (URLs, env var names) need `overflow-wrap: anywhere` (or
  `word-break: break-word`); `white-space: nowrap` on headers/summaries with
  `text-overflow: ellipsis; overflow: hidden` keeps single-line labels single-line.
- `overflow: hidden` on a rounded frame clips children to the border-box, not the
  rounded shape, unless the element also establishes clipping via `border-radius` on
  the same element (it does) — but *fixed-position* descendants (the WebGL canvas) are
  never clipped by an ancestor's overflow; they need `clip-path`/`mask` themselves.

## 3. Sticky, scroll containers and fixed layers

- `position: sticky` sticks only within its *containing block* (the nearest scrolling
  ancestor is the scrollport; the parent box bounds the travel). If the sticky element
  is the tallest thing in its grid/flex area there is no room to stick and it scrolls
  away — that is why the Music panel's stage didn't stay put.
- `position: fixed` is relative to the viewport, or to the nearest ancestor with a
  `transform`, `filter`, `perspective` or `will-change: transform` (this app's 3D
  carousel uses transforms, so "fixed" inside a panel is really "absolute to the panel").
- Scroll-driven repositioning of overlays (the device canvas) must listen to the actual
  scroller's `scroll` event (passive, rAF-throttled), not `window`.

## 4. Masks, clips and layering

- `mask-image: linear-gradient(...)` fades content at a scroller's edge; it applies to the
  element's own painting only, so a fixed overlay above it keeps a hard edge.
- `clip-path: inset(...)` is cheap and animatable; `overflow: hidden` is not a clip for
  transformed or fixed descendants.
- Stacking: `z-index` only competes within the same stacking context; `transform`,
  `opacity < 1`, `filter`, `will-change`, `isolation: isolate` each create one. When a
  `z-index` "doesn't work", find which ancestor started a new context.

## 5. Responsive rules that hold

- Breakpoints target *layout* changes, not devices: collapse a two-column deck when the
  content column would drop below ~360px, not at "tablet width". Use `max-width:` queries
  in CSS px and test the breakpoint ±20px.
- **Container queries** (`container-type: inline-size` on the parent, `@container
  (max-width: 600px)`) let a card react to *its* width, which is what a component inside
  a resizable panel needs. Supported in all current browsers.
- Reserve space for fixed chrome (nav rail, status bar, footer) via CSS variables and
  subtract them in `min()`/`calc()` — measure the chrome in the browser, do not guess.
- `aspect-ratio` + `max-width: 100%` keeps media boxes stable; never set `min-width`
  wider than the viewport on anything.
- Provide `@media (prefers-reduced-motion: reduce)` for every infinite animation.

## 6. Cascade, specificity and variables

- Later rules win only at equal specificity; `.carousel-panel h2` (0,1,1) beats
  `h2` (0,0,1) wherever it is in the file. Prefer the lowest specificity that works;
  never reach for `!important` to beat your own rules — restructure the selector.
- `@layer` can order whole blocks (`@layer base, components, panels;`) so panel overrides
  always win without specificity wars.
- Custom properties inherit and can be redefined per breakpoint (`:root { --panel-w }`
  inside a media query), which keeps size and offset math in one place — the pattern
  used for `--panel-w`/`--panel-h`. Read a variable in JS with
  `getComputedStyle(el).getPropertyValue('--x')` instead of duplicating the number.
- `details:not([open])` styles the collapsed state; `summary::marker` /
  `summary::-webkit-details-marker` control the disclosure triangle.

## 7. Verify like a user

- Test at 780×780, 1024×768, 1280×800 and 1280×900; scroll every panel to the bottom.
- Assert geometry in the browser (`getBoundingClientRect`) rather than eyeballing:
  "every card's right edge is ≥ 8px inside the scroller", "summary height ≤ 1.6 × font-size".
- Check `getComputedStyle(el).fontSize` when text looks wrong — it tells you which unit
  chain produced the number.
- Use the `ui-testing` skill's probe for screenshots and console/network errors.
