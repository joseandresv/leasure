# Leasure DDR Arcade — design conventions

This design system is **tokens + idiom only** (no component library). Build every screen from the CSS custom properties below — they are the entire design language of a DDR-arcade-cabinet music app. Read `styles.css` and `tokens/tokens.css` before styling; every token named here exists there.

## Setup

No provider or wrapper. Apply the arcade void to the page root exactly like this — flat `--bg-base` alone is off-brand:

```css
body {
  font-family: var(--font-ui);
  background-color: var(--bg-base);
  background-image: var(--energy-slash), var(--tech-grid),
    radial-gradient(120% 80% at 50% -10%, rgba(31,107,255,0.16), transparent 60%);
  background-attachment: fixed;
  color: var(--text-primary);
}
```

## Styling idiom: CSS custom properties, never raw hex

- **Surfaces**: `--bg-base` (page void) → `--bg-surface` (cards) → `--bg-elevated` (raised controls); `--bg-dark` for LED wells; `--bg-navy-glass` gradient for panels. Panels get `box-shadow: var(--shadow-panel)` and a faint cyan border `1px solid rgba(56,214,255,0.18)`.
- **Structure color is cyan/blue**: `--accent-blue` (#38d6ff neon cyan) for section labels, borders, links, focus; `--accent-blue-deep` for fills. `--accent-magenta` sparingly for "hot" emphasis.
- **Data is LED-lit**: numeric readouts use `--font-mono` in `--text-led` (amber) or `--text-led-green` with `text-shadow: 0 0 10px var(--text-led-glow)`, inside a `--bg-dark` well with `box-shadow: var(--shadow-inset)`.
- **Text ramp**: `--text-primary` / `--text-secondary` / `--text-muted`. Status: `--color-success`, `--color-error`, `--color-warning`.
- **Type roles** — never swap them: `--font-display` (Orbitron, uppercase, `letter-spacing: 0.14em+`) for titles and big numerics; `--font-pixel` (Press Start 2P, 8–10px only) for tiny HUD labels; `--font-mono` (JetBrains Mono) for data; `--font-ui` (Inter) for body.
- **The signature shape**: sheared plates. `transform: skewX(var(--plate-shear))` on the container, counter-skew children with `transform: skewX(calc(-1 * var(--plate-shear)))`. Chrome edges use `--chrome-face` / `--chrome-bevel` gradients.
- **Buttons feel like hardware**: raised = `--bg-elevated` + `var(--shadow-raised)`; pressed = `var(--shadow-pressed)`. Tight radii only: `--radius-sm/md/lg` (3/5/8px).
- **Motion is mechanical**: snap into place with `var(--transition-snap)` (240ms detent overshoot); `--ease-relay` for switches, `--ease-weighted` for heavy masses. Never default `ease` on interactive elements.
- **Spacing**: `--space-xs/sm/md/lg/xl` (0.25–2rem).

## Idiomatic example — a track panel

```html
<div style="background:var(--bg-navy-glass); box-shadow:var(--shadow-panel);
     border:1px solid rgba(56,214,255,0.18); transform:skewX(var(--plate-shear)); padding:12px 26px;">
  <div style="transform:skewX(calc(-1 * var(--plate-shear)));">
    <h2 style="font-family:var(--font-display); font-weight:800; text-transform:uppercase;
        letter-spacing:0.14em;">Rhythm Deck</h2>
    <span style="font-family:var(--font-mono); color:var(--text-led);
        text-shadow:0 0 10px var(--text-led-glow); background:var(--bg-dark);
        box-shadow:var(--shadow-inset); padding:6px 12px; border-radius:var(--radius-sm);">
      03:42 · 320kbps</span>
  </div>
</div>
```

See `guidelines/style-guide.md` for the full idiom rationale and the Foundations preview cards for rendered reference.
