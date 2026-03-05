# InfraForge — UI Style Guide

> **Consult this before any frontend change.** It captures the design tokens, component
> patterns, and layout conventions used across the app. When adding new UI, follow these
> patterns. When modifying existing UI, verify consistency before and after.

**Source files:**
- `static/styles.css` — All styles (single file, ~16k lines)
- `static/app.js` — All JS + HTML templates (single file)
- `static/index.html` — App shell HTML

---

## 1. Design Tokens

All tokens are CSS custom properties defined in `:root` (styles.css lines 4-46).

### Backgrounds (elevation scale, darkest to lightest)

| Token | Hex | Usage |
|-------|-----|-------|
| `--bg-primary` | `#0d1117` | Page background, detail overlay background |
| `--bg-secondary` | `#161b22` | Cards, sidebar, panels, section containers |
| `--bg-tertiary` | `#1c2128` | Nested items within cards, code viewers, form inputs |
| `--bg-elevated` | `#21262d` | Badges, tags, elevated controls |
| `--bg-hover` | `#292e36` | Hover states on secondary backgrounds |

**Rule:** Cards sit on `--bg-secondary`. Items nested inside cards use `--bg-tertiary`.
Code viewers and form inputs use `--bg-tertiary`. Never put `--bg-secondary` items inside
a `--bg-secondary` container — they'll be invisible.

### Text

| Token | Hex | Usage |
|-------|-----|-------|
| `--text-primary` | `#e6edf3` | Headings, primary content, interactive labels |
| `--text-secondary` | `#8b949e` | Descriptions, secondary labels, muted content |
| `--text-muted` | `#6e7681` | Timestamps, hints, disabled text |

### Accent Colors (semantic)

| Token | Hex | Meaning |
|-------|-----|---------|
| `--accent-blue` | `#58a6ff` | Interactive, informational, links, focus rings, primary buttons |
| `--accent-green` | `#3fb950` | Success, approved, connected, passed |
| `--accent-red` | `#f85149` | Error, failed, disconnected, not approved |
| `--accent-orange` | `#d29922` | Warning, conditional, deprecated, in-progress |
| `--accent-purple` | `#bc8cff` | Decorative, Copilot branding |
| `--accent-cyan` | `#39d2c0` | Decorative, sidebar branding gradient |

**Aliases** (point to the same values, used in some older code):
- `--border-color` = `#30363d` (alias for `--border-default`)
- `--accent-primary` = `#58a6ff` (alias for `--accent-blue`)

### Borders

| Token | Hex | Usage |
|-------|-----|-------|
| `--border-default` | `#30363d` | Standard card/section borders, dividers |
| `--border-muted` | `#21262d` | Subtle separation (same as `--bg-elevated`) |
| `--border-subtle` | `#21262d` | Same as muted; used in nested item borders |

### Border Radius

| Token | Value | Usage |
|-------|-------|-------|
| `--radius-sm` | `6px` | Form inputs, small badges, buttons (xs) |
| `--radius-md` | `8px` | **Standard for all cards and sections**, buttons (default) |
| `--radius-lg` | `12px` | Modals, large containers |

**Rule:** Use `--radius-md` (8px) for section cards and most containers. Use `--radius-sm`
for form inputs. Use `--radius-lg` for modal dialogs only.

### Shadows

| Token | Value | Usage |
|-------|-------|-------|
| `--shadow-sm` | `0 1px 3px rgba(0,0,0,0.3)` | Subtle lift |
| `--shadow-md` | `0 4px 12px rgba(0,0,0,0.4)` | Buttons on hover, toasts |
| `--shadow-lg` | `0 8px 24px rgba(0,0,0,0.5)` | Modals |

### Typography

| Token | Value |
|-------|-------|
| `--font-sans` | `-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif` |
| `--font-mono` | `'Cascadia Code', 'Fira Code', 'JetBrains Mono', Consolas, monospace` |

### Layout

| Token | Value |
|-------|-------|
| `--sidebar-width` | `280px` |
| `--header-height` | `56px` |

---

## 2. Section Card Pattern

**Every visible section on the template detail page must use this pattern:**

```css
.my-section {
    background: var(--bg-secondary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    padding: 1rem;
}
```

Exceptions:
- **Code viewers** (ARM template viewer) use `--bg-tertiary` background since darker works
  better for code.
- **Accent sections** (Request Changes) may use a colored border like
  `border: 1px solid var(--accent-primary)` to draw attention.
- **Status banners** (CTA) are self-contained with their own colored backgrounds and don't
  need an outer card wrapper.

Items nested inside cards (e.g., version log items, pipeline run items) use:

```css
.nested-item {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);    /* or 4px for small items */
}
```

---

## 3. Buttons

### Base: `.btn`

```
padding: 0.625rem 1.25rem
border: 1px solid var(--border-default)
border-radius: var(--radius-md)
font-size: 0.875rem (14px)
font-weight: 500
transition: all 0.15s ease
```

### Variants

| Class | Background | Text | Border | Use for |
|-------|-----------|------|--------|---------|
| `.btn-primary` | `--accent-blue` | white | `--accent-blue` | Primary actions (Deploy, Validate) |
| `.btn-secondary` | `--bg-elevated` | `--text-primary` | `--border-default` | Secondary actions |
| `.btn-ghost` | transparent | `--text-secondary` | transparent | Tertiary/inline actions |
| `.btn-outline` | transparent | `--accent-blue` | `--accent-blue` | Alternative primary style |
| `.btn-accent` | `--accent-green` | white | `--accent-green` | Positive/confirm actions |
| `.btn-danger` | transparent | `--accent-red` | default | Destructive actions |

### Sizes

| Class | Padding | Font size |
|-------|---------|-----------|
| (default) | `0.625rem 1.25rem` | `0.875rem` (14px) |
| `.btn-sm` | `0.375rem 0.75rem` | `0.8125rem` (13px) |
| `.btn-xs` | `0.25rem 0.625rem` | `0.75rem` (12px) |

### Hover behavior

Primary/accent buttons: `translateY(-1px)` + `box-shadow: var(--shadow-md)`.
Secondary/ghost buttons: background shifts to `--bg-hover`.

---

## 4. Badges & Status Indicators

### Status badge pattern

All status badges use a consistent formula:

```css
.status-badge {
    padding: 0.1875rem 0.5rem;
    border-radius: 100px;          /* pill */
    font-size: 0.6875rem (11px);
    font-weight: 500;
}
```

Color formula: `background: rgba(color, 0.12-0.15)` + `color: accent-var` + optional
`border: 1px solid rgba(color, 0.25-0.30)`.

### Status-to-color mapping

| Status | Color | Accent variable |
|--------|-------|----------------|
| approved | Green | `--accent-green` |
| passed | Blue | `--accent-blue` |
| validated | Teal/Green | `#059669` |
| draft | Gray | `--text-secondary` |
| conditional | Orange | `--accent-orange` |
| under_review | Blue | `--accent-blue` |
| not_approved | Red | `--accent-red` |
| failed | Red | `--accent-red` |
| deprecated | Orange | `--accent-orange` |
| offboarded | Gray | `--text-secondary` |

### Other badge types

| Class | Style | Usage |
|-------|-------|-------|
| `.category-badge` | Pill, `--bg-elevated` bg, `--border-default` border | Format, category labels |
| `.region-tag` | Small rect, `--bg-elevated` bg, mono font | Tags, regions |
| `.risk-badge` | Text-only colored (green/orange/red by risk level) | Risk ratings |
| `.svc-id` | `--text-muted`, mono font, no background | Service ID display |

---

## 5. Form Inputs

```css
/* Standard input treatment */
input, select, textarea {
    padding: 0.5rem 0.75rem;
    background: var(--bg-tertiary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);    /* 6px */
    color: var(--text-primary);
    font-size: 0.8125rem;
}

/* Focus ring */
:focus {
    outline: none;
    border-color: var(--accent-blue);
    box-shadow: 0 0 0 2px rgba(88, 166, 255, 0.15);
}
```

- Labels: `font-size: 0.8125rem`, `font-weight: 500`, `color: var(--text-primary)`
- Hints: `color: var(--text-muted)`, `font-weight: 400`, `font-size: 0.75rem`
- Required indicator: `color: var(--accent-red)` on a `.required` span
- Checkboxes: `accent-color: var(--accent-blue)`
- Toggle switches: `.std-toggle` — 36x20px iOS-style with green active state

---

## 6. Toasts

```css
.toast {
    position: fixed;
    bottom: 2rem;
    right: 2rem;
    padding: 0.75rem 1.25rem;
    border-radius: var(--radius-md);
    font-size: 0.8125rem;
    z-index: 2000;
    box-shadow: var(--shadow-md);
    animation: toastSlideIn 0.3s ease, toastFadeOut 0.3s ease 2.7s forwards;
}
```

Two variants: `.toast-success` (green) and `.toast-error` (red).
JS function: `showToast(message, type, duration)`.

---

## 7. Modals & Overlays

### Standard modal

```css
.modal-overlay {
    position: fixed; inset: 0;
    background: rgba(0, 0, 0, 0.65);
    backdrop-filter: blur(4px);
    z-index: 1000;
}
.modal-dialog {
    background: var(--bg-secondary);
    border-radius: var(--radius-lg);   /* 12px */
    max-width: 560px;
    box-shadow: var(--shadow-lg);
}
```

Structure: `.modal-header` (border-bottom) + `.modal-body` (scrollable) + `.modal-footer`
(border-top, flex-end).

### Detail panel overlay

Full-page takeover at z-index 500. Opaque `--bg-primary` background (not semi-transparent).
Content centered at max-width 1200px. Animation: `panelFadeIn` (0.2s, translateY 12px).

### Z-index hierarchy

| Layer | z-index | Component |
|-------|---------|-----------|
| Detail panel | 500 | `.detail-panel-overlay` |
| Standard modal | 1000 | `.modal-overlay` |
| Pipeline overlay | 1100 | `.pipeline-overlay` |
| Toast | 2000 | `.toast` |
| Special modals | 9000-9999 | Remediation, upgrade analysis |

---

## 8. Collapsible Sections

**Two patterns exist. Prefer Pattern B for new code.**

### Pattern A: JS class toggle (legacy)

Used by: Version Log (`.comp-verlog-section`)

```html
<div class="my-section">
    <h4 class="my-toggle" onclick="this.parentElement.classList.toggle('my-open')">
        Title <span class="my-arrow">▸</span>
    </h4>
    <div class="my-body">...</div>
</div>
```

Body hidden via `display: none`, shown via `.my-open .my-body { display: block }`.
Arrow rotates 90deg on open.

### Pattern B: Native `<details>/<summary>` (preferred)

Used by: Service details, deploy sections, draft history

```html
<details class="my-toggle">
    <summary>Title</summary>
    <div class="my-body">...</div>
</details>
```

```css
.my-toggle summary::before {
    content: '\25B8';                  /* right-pointing triangle ▸ */
    transition: transform 0.2s ease;
}
.my-toggle[open] summary::before {
    transform: rotate(90deg);
}
```

---

## 9. App Layout

### Shell structure

```
#app-screen (flex, 100vh)
  aside.sidebar (width: 280px, --bg-secondary)
    .sidebar-header
    .sidebar-nav (flex: 1, scrollable)
    .sidebar-footer
  main.main-content (flex: 1)
    .content-header (height: 56px, --bg-secondary, border-bottom)
    [page content area - scrollable]
```

### Template catalog grid

```css
.template-cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 1rem;
}
```

Cards: `--bg-secondary`, 4px colored left border (by status), `8px` radius, flex column.

### Template detail panel

Full-viewport overlay. Inner content: max-width 1200px, two-column grid (`1fr 380px`).
Collapses to single column at 768px.

### Responsive breakpoints

| Breakpoint | What changes |
|------------|-------------|
| 900px | ARM editor split view stacks vertically |
| 768px | **Primary:** sidebar hidden, layouts go single-column, padding reduced |
| 800px | Analytics grid goes single-column |

---

## 10. Icons

**All icons are emoji.** No icon font or SVG sprite. Use native Unicode emoji characters
directly in HTML:

```html
<span class="nav-icon">emoji-here</span>
```

The one exception is the Microsoft logo on the login screen (inline SVG).

---

## 11. Template Detail Page Sections

The template detail page renders these sections top-to-bottom. Each must follow the
section card pattern from Section 2.

| Section | CSS Class | Card? | Visibility |
|---------|-----------|-------|------------|
| Meta bar | `.detail-meta` | No (uses border-bottom) | Always |
| Status CTA | `.tmpl-test-cta` | Self-contained banner | Always |
| ARM viewer | `.tmpl-arm-viewer-section` | Yes (`--bg-tertiary`) | Always (collapsed) |
| Validation form | `.tmpl-validate-section` | Yes | Hidden by default |
| Deploy form | `.tmpl-deploy-section` | Yes | Hidden by default |
| Composed From | `.comp-hero-section` | Yes | Always |
| Request Changes | `.tmpl-revision-section` | Yes (accent border) | Always |
| Compliance scan | `#tmpl-scan-results` | Yes (when non-empty) | Hidden by default |
| Pipeline runs | `#tmpl-pipeline-runs-container` | Yes | Hidden by default |
| Version log | `.comp-verlog-section` | Yes | Always (collapsed) |

**Rules:**
- Do not duplicate information already shown in `.detail-meta` in any section header.
- When adding a new section, add it to this table and give it the standard card treatment.
- Hidden sections that appear conditionally must still have card styling applied so they
  look correct when revealed.

---

## 12. Color Application Rules

### Status badges
Use the badge formula: translucent background (12-15% opacity of accent) + solid accent
text + optional translucent border (25-30% opacity).

### Cards with status left-border
Template cards and service items use a 3-4px left border colored by status.

### Interactive elements
- Default state: `--border-default` border
- Hover: `border-color: var(--accent-blue)` or `background: var(--bg-hover)`
- Focus: `border-color: var(--accent-blue)` + `box-shadow: 0 0 0 2px rgba(88, 166, 255, 0.15)`
- Active/selected: `--accent-blue` background or border

### Alert/banner sections
Banner backgrounds use the same rgba formula as badges but at lower opacity (8% instead
of 12-15%):
- Success: `rgba(green, 0.08)` + green text + `rgba(green, 0.2)` border
- Error: `rgba(red, 0.08)` + red text + `rgba(red, 0.2)` border
- Warning: `rgba(orange, 0.08)` + orange text + `rgba(orange, 0.2)` border
- Info: `rgba(blue, 0.08)` + blue text + `rgba(blue, 0.2)` border

---

## 13. Spacing Reference

The app follows an approximate 4px base grid with common stops at:

`0.25rem (4px) / 0.375rem (6px) / 0.5rem (8px) / 0.75rem (12px) / 1rem (16px) / 1.25rem (20px) / 1.5rem (24px) / 2rem (32px)`

- **Card padding:** `1rem` standard, `0.75rem 1rem` for compact sections
- **Section spacing:** `margin-bottom: 1.25rem` between sections
- **Button padding:** `0.625rem 1.25rem` (default), `0.375rem 0.75rem` (sm)
- **Form input padding:** `0.5rem 0.75rem`
- **Grid gaps:** `1rem` (catalog grid), `2rem` (detail layout), `0.5rem` (badge containers)
