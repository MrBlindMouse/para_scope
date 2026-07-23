## In-Depth Analysis &amp; Style Guide (2026 Edition)

**Source Basis**: This guide is synthesized from the official 37signals documentation (primarily the April 2024 “Modern CSS patterns in Campfire” deep-dive by Jason Zimdars) and analysis of their open-source Fizzy codebase (the most recent public reference for their production patterns). It also incorporates evolutionary refinements seen in Campfire, Writebook, and Fizzy. All products use **pure vanilla CSS**—no Sass, PostCSS, Tailwind, or any build step. The philosophy is “modern CSS is plenty.”

This is the battle-tested system used in shipping 37signals products (Basecamp, HEY, Campfire, Writebook, Fizzy). It scales for small teams while remaining readable, maintainable, and high-velocity.

## 1. Philosophy &amp; Core Principles

- **Vanilla-first, no-build**: Write standard CSS. Serve it directly via Propshaft (or equivalent). No compilation, no bundling, no JavaScript-generated styles. Leverage the platform maximally.
- **Evergreen browsers only**: Target modern features supported in Chrome/Edge/Safari/Firefox (CSS nesting, `:has()`, `:is()`, `:where()`, OKLCH colors, container queries, `@starting-style`, `color-mix()`, logical properties, View Transitions, etc.).
- **Embrace the cascade**: Use `@layer`, custom properties, and modern selectors to *work with* the cascade instead of fighting it.
- **Custom properties as component APIs**: Make every component themeable and variant-friendly via CSS variables. Variants become one-line overrides.
- **DRY + readable**: Minimal duplication. Self-documenting code. HTML stays clean (no class spam).
- **Content-driven design**: Prefer `ch` units, `clamp()`, container queries, and `:has()` over rigid breakpoints or JavaScript-driven state.
- **Minimal utilities**: A small, purposeful set (not Tailwind-style). Most styling lives in semantic component files.
- **Performance &amp; maintainability**: ~14,000 lines across ~105 flat files in Fizzy (as of late 2025). Fast loads, easy navigation, zero specificity wars.

**Goal**: Ship delightful UIs with the smallest possible cognitive and tooling overhead.

## 2. File Structure &amp; Organization

Flat, simple, and self-explanatory (under `app/assets/stylesheets/` in Rails apps, or equivalent root).

Typical structure:

```
app/assets/stylesheets/
├── _reset.css
├── base.css
├── colors.css          (or :root vars in a global file)
├── utilities.css       (minimal purposeful utilities)
├── layout.css
├── buttons.css
├── inputs.css
├── cards.css
├── dialogs.css
├── spinners.css
├── animations.css
├── [every-other-component-or-feature].css
└── ...
```

- **One file per concept/component** (usually 100–300 lines).
- No subfolders or deep import chains.
- Import order is controlled by `@layer` (see below), not file order.
- Easy to find: Want button styles? Open `buttons.css`.

## 3. Cascade Layers (`@layer`)

Explicit specificity control is a cornerstone.

At the top of your main stylesheet (or imported entry point):

```css
@layer reset, base, layout, components, utilities;
```

Example layers:

```css
@layer reset {
  *, *::before, *::after { box-sizing: border-box; }
}

@layer base {
  body { font-family: system-ui, sans-serif; line-height: 1.5; }
}

@layer components {
  /* All your .btn, .card, etc. live here */
}

@layer utilities {
  .flex { display: flex; }
  /* … */
}
```

**Why it works**: Later layers always win, regardless of selector specificity. No more `!important` hacks or specificity wars.

## 4. Color System (OKLCH + Custom Properties)

Perceptually uniform, wide-gamut (Display-P3), easy dark mode.

### 4.1 Raw LCH values (in `:root` or `colors.css`)

```css
:root {
  /* Greys */
  --lch-gray:        96% 0.005 96;
  --lch-gray-dark:   92% 0.005 96;
  --lch-gray-darker: 75% 0.005 96;

  /* Blues (example) */
  --lch-blue:        54% 0.23 255;
  --lch-blue-light:  95% 0.03 255;
  --lch-blue-dark:   80% 0.08 255;
}
```

### 4.2 Semantic colors (wrapping `oklch()`)

```css
:root {
  --color-canvas:          oklch(var(--lch-gray));
  --color-ink:             oklch(var(--lch-gray-dark));
  --color-border:          oklch(var(--lch-gray-darker));
  --color-link:            oklch(var(--lch-blue));
  --color-negative:        oklch(65% 0.25 20); /* example red */
}
```

**Dark mode** (simple and automatic):

```css
@media (prefers-color-scheme: dark) {
  :root {
    --lch-gray: 20% 0.0195 232.58; /* flip lightness values */
    /* … update all base LCH vars */
  }
}
```

Or use `html[data-theme="dark"]` for manual toggling.

**Dynamic mixing** (Fizzy pattern):

```css
.card {
  --card-bg: color-mix(in srgb, var(--card-color) 4%, var(--color-canvas));
}
```

## 5. Custom Properties as Component APIs

The single most powerful pattern.

### 5.1 Inline fallbacks (preferred modern style)

```css
.btn {
  background-color: var(--btn-background, var(--color-canvas));
  color: var(--btn-color, var(--color-ink));
  padding: var(--btn-padding, 0.5em 1.1em);
  border-radius: var(--btn-border-radius, 99rem);
}
```

### 5.2 Variants (override one variable)

```css
.btn--reversed {
  --btn-background: var(--color-ink);
  --btn-color: var(--color-canvas);
}

.btn--negative {
  --btn-background: var(--color-negative);
}
```

**Benefits**:

- No rule duplication.
- Easy theming/dynamic updates via JavaScript (`element.style.setProperty('--btn-background', newValue)`).
- Self-documenting: the API for the component is visible in its CSS file.

## 6. Naming Convention (Pragmatic BEM-inspired)

- **Block**: `.card`, `.btn`
- **Element**: `.card__header`, `.btn__icon`
- **Modifier**: `.card--featured`, `.btn--reversed`

**Loose rules** (not religious BEM):

- Keep chains shallow.
- Use semantic, readable names.
- Heavy reliance on custom properties means fewer modifier classes are needed.

## 7. Key Modern Selectors &amp; Patterns

### 7.1 `:has()` – The game-changer (parent selector)

```css
/* Icon-only circular button */
.btn:where(:has(.for-screen-reader):has(img)) {
  --btn-border-radius: 50%;
  --btn-padding: 0;
  aspect-ratio: 1;
  inline-size: var(--btn-size);
  block-size: var(--btn-size);
  display: grid;
  place-items: center;
}

/* Unread indicator on closed sidebar */
#sidebar:where(:not([open]):has(.unread))::after {
  /* dot styles */
}

/* Dynamic row dimming */
.membership-item:has(.btn.invisible) { opacity: 0.5; }
```

### 7.2 Native nesting

```css
.btn {
  /* base */

  @media (any-hover: hover) {
    &:hover { filter: brightness(var(--btn-hover-brightness, 1.1)); }
  }

  &[disabled] {
    opacity: 0.3;
    cursor: not-allowed;
  }
}
```

### 7.3 Container queries &amp; logical properties

```css
.card__content { contain: inline-size; }

@container (width < 300px) {
  .card__meta { flex-direction: column; }
}

.pad-block { padding-block: var(--block-space); }
```

### 7.4 `@starting-style` for smooth animations (dialogs, etc.)

```css
.dialog {
  opacity: 0;
  transform: scale(0.2);
  transition: 150ms allow-discrete;
  transition-property: display, opacity, overlay, transform;

  &[open] {
    opacity: 1;
    transform: scale(1);
  }

  @starting-style {
    &[open] { opacity: 0; transform: scale(0.2); }
  }
}
```

## 8. Utilities (Minimal &amp; Purposeful)

~60 focused classes in `utilities.css` (examples):

```css
@layer utilities {
  .flex { display: flex; }
  .gap { gap: var(--inline-space, 1ch); }
  .stack { display: flex; flex-direction: column; }
  .pad { padding: var(--block-space) var(--inline-space); }
  .visually-hidden { /* classic screen-reader only */ }
  .txt-center { text-align: center; }
}
```

Use sparingly—prefer component classes.

## 9. Responsive &amp; Spacing Strategy

- **Horizontal**: `ch` units (`--inline-space: 1ch`) → content-aware.
- **Vertical**: `rem` or custom props (`--block-space: 1rem`).
- **Breakpoints**: Minimal and content-driven, e.g., `@media (min-width: 100ch)`.
- **Typography**: `clamp()` + responsive custom properties.

## 10. Additional Patterns from Production Use

- **Spinners**: Pure CSS with masks and `currentColor`.
- **Text highlighting**: Custom `.circled-text` with pseudo-elements and `mix-blend-mode`.
- **View Transitions API**: For page-level animations.
- **Dynamic theming**: JS updates root custom properties; everything reacts automatically.

## 11. Quick-Start Template (Copy-Paste Ready)

```css
@layer reset, base, layout, components, utilities;

/* colors.css or :root */
:root { /* all --lch-* and --color-* vars */ }

/* components/buttons.css */
.btn {
  --btn-background: var(--color-canvas);
  --btn-color: var(--color-ink);
  --btn-padding: 0.5em 1.1em;
  /* … */

  background-color: var(--btn-background);
  color: var(--btn-color);
  padding: var(--btn-padding);
}

.btn--reversed { --btn-background: var(--color-ink); --btn-color: var(--color-canvas); }
```

## 12. When &amp; Why This Wins (Analysis)

**Strengths**:

- Extremely low overhead → fastest iteration for small/medium teams.
- Future-proof: every new CSS feature (2024–2026) slots in perfectly.
- Readable for designers *and* developers.
- Eliminates most historical CSS pain (specificity, duplication, dark mode, responsiveness).
- Proven in multiple production products.

**Trade-offs**:

- Requires comfort with modern CSS (not ideal for very junior teams).
- Less “guardrails” than strict BEM + ITCSS for massive enterprise monorepos.
- Flat structure can feel chaotic if you exceed ~100 files (rare in 37signals-style teams).
