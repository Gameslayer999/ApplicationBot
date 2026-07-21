# ApplicationBot — UI / UX Guidelines

Living reference for ApplicationBot's local web + native desktop UI. Token values below are taken
from the shipped interface. Industry practices are synthesized from Apple HIG, Figma design-system
guidance, Material Design 3, Microsoft Fluent 2, WCAG 2.2 AA, and Nielsen Norman Group heuristics —
adapted for ApplicationBot's local-first, single-user operator tool (not a consumer mobile app or a
marketing site).

| Source of truth | Path |
|-----------------|------|
| **Rules (UI Design Principles + AI Coding Guidelines)** | [CLAUDE.md](CLAUDE.md) |
| **How the UI is built (the "how")** | [ui.md](ui.md) |
| Design tokens & all HTML/CSS/JS | [applicationbot/web.py](applicationbot/web.py) (`:root { … }` and the inline `<style>`/`<script>`) |
| Native desktop shell | [applicationbot/app.py](applicationbot/app.py) (pywebview window) |
| Setup readiness checks (drives onboarding) | [applicationbot/doctor.py](applicationbot/doctor.py) |

Expand this file when durable visual or interaction decisions land — prefer updating here over
rediscovering in chat. When this file and [ui.md](ui.md) overlap, `ui.md` is the implementation
detail; this file is the design vocabulary.

---

## 1. Product posture

ApplicationBot is a **local-first, single-user operator tool** — a job-application pipeline you run
on your own machine — not a marketing splash page and not a multi-tenant SaaS.

- Clean, calm, high-contrast chrome; shadcn "zinc" neutrals (near-black dark, zinc-50 light) with
  one restrained blue accent.
- Dense where the operator needs scan speed (the tracker table, funnel, token spend); spacious
  where a first-run user completes setup and profile onboarding.
- Prefer **tokens over one-off hex** in every rule (see §14) — the same string powers light, dark,
  and the native window.
- Prefer **semantic roles** (accent action, danger, success, warning) over inventing new accent
  colors per screen.
- The **same UI renders in two shells** — a browser tab and a native pywebview window. Never write
  shell-specific UI; style for a standard webview and it works in both.

### Anti-patterns (do not default to)

- Purple-on-white or purple→indigo gradient themes
- Warm cream backgrounds with terracotta + display serif heroes
- Glow stacks, neon borders, emoji as the *only* status signal
- Flat single-color pages with no surface hierarchy
- Color as the **only** status signal
- Decoration that competes with the résumé / job content (clarity & deference — §10)

---

## 2. Industry design principles

Use these as a shared vocabulary when reviewing screens. Prefer ApplicationBot tokens and patterns
when a platform-specific HIG conflicts with the shipped UI.

### Apple Human Interface Guidelines (2026 principles)

From [Apple Design principles](https://developer.apple.com/design/human-interface-guidelines/design-principles) / WWDC26 "Principles of great design":

| Principle | Meaning for ApplicationBot |
|-----------|----------------------------|
| **Purpose** | Every screen should answer "what is the user here to finish?" — set up a profile, tailor a résumé, unblock an application, review the tracker. Cut chrome that doesn't serve that job. |
| **Agency** | Stay out of the way; don't lock users into modes. Always provide Cancel / Escape / click-out from the setup overlay and any modal. Build forgiveness (dry-run default, skippable onboarding, confirm destructive). |
| **Responsibility** | Be clear about what leaves the machine. The résumé, contact details, and site logins are the user's; only send to Claude what a task needs, and say when you drop/skip input (§10). |
| **Familiarity** | Things that look the same should work the same. Reuse the one sidebar, one button style, one modal pattern, one spinner across every tab. |
| **Flexibility** | Support keyboard, zoom, dense vs spacious views, light/dark. Adapt density for the tracker table vs a first-run profile. |
| **Simplicity** | Strip the unnecessary so the core task shows. One primary action per view; progressive disclosure for long flows (setup checklist, collapsible profile cards). |
| **Craft** | Consistent spacing, alignment, focus rings, loading states — polish that builds trust when the app applies to jobs on the user's behalf. |
| **Delight** (where appropriate) | Subtle motion and clear success feedback ("Saved ✓") — never novelty that distracts from an irreversible submit. |

**UI layer vs content layer** (Apple): keep **navigation chrome quiet**; put the accent color and
product personality mainly in **content** (status, actions, data). ApplicationBot already does this:
neutral sidebar/chrome = UI layer; the tailored résumé, funnel, and tracker = content.

### Figma design-system practices

From [Figma Design Systems 102](https://www.figma.com/blog/design-systems-102-how-to-build-your-design-system/), [components & libraries](https://www.figma.com/best-practices/components-styles-and-shared-libraries/), and [design tokens](https://www.figma.com/resource-library/design-tokens/):

1. **Principles first** — small, memorable rules (this doc) before new components.
2. **Token hierarchy** — name for **role**, not appearance (`--accent-text`, `--muted`, not
   `--blue` / `--gray`). See §14.
3. **Semantic naming aligned with code** — CSS var names should match what you'd call the thing.
4. **Document usage** — when a durable visual rule ships, update this file (and [ui.md](ui.md)).
5. **Accessibility in foundations** — contrast and target size are part of the system, not a late
   QA pass (there is a contrast checker in the repo — §5, [ui.md](ui.md)).
6. **Variants for structure; properties for content** — button *role* (accent/ghost/danger) ≠
   button *state* (hover/loading).

### Material Design 3 & Microsoft Fluent 2

| Idea | Practice here |
|------|----------------|
| **Color roles / hierarchy** (M3) | Accent for the highest-priority action only; quiet borders/ghost for supporting actions; surfaces for cards/backgrounds — don't paint every control accent blue. |
| **Type roles** (M3) | Distinct roles by weight/size (heading vs body vs uppercase meta label) — don't rely on color alone for hierarchy. |
| **Proximity & whitespace** (Fluent) | Closer = related; more space = more importance or separation. Prefer spacing over extra divider lines. |
| **4px-ish base rhythm** (Fluent / M3) | Keep spacing on a consistent small-step rhythm; avoid random `13px` paddings. |
| **Touch / pointer targets** | WCAG floor **24×24**; Apple/Fluent ideal **44**. This is a desktop web app, so 24px is the bar; go bigger for anything a phone user might tap. |
| **Responsive techniques** (Fluent) | Reposition, resize, reflow, show/hide, re-architect — see §8. |

---

## 3. Design-source of truth

ApplicationBot has no external brand system (no Confluence/Zeplin/XD). The **shipped UI is the
brand**: the design tokens, type stack, and component classes all live in one embedded string in
[applicationbot/web.py](applicationbot/web.py), and the interaction rules live in
[CLAUDE.md](CLAUDE.md) → **UI Design Principles**. When something visual is decided, encode it as a
token or a class there, then document it here and in [ui.md](ui.md).

### Type

There is **one** system font stack — no bespoke display font, no web-font download (keeps the local
app fast and offline-friendly):

```css
font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
```

Weights in use: Regular (body), 600 (UI labels / emphasis / active tab), 800 (brand mark, strong
headings). Don't introduce a second family.

One exception: a **monospace** stack, `--mono` (`ui-monospace, SFMono-Regular, "SF Mono", Menlo,
Consolas, "Liberation Mono", monospace`), used **only** for metrics/data (count-pill numbers,
funnel figures, token tables, fit chart) and the live-run log stream — never for prose.

### Icons / glyphs

Chrome uses **lucide-style inline SVG stroke icons** (24×24 viewBox, `stroke-width:2`, round
caps/joins, `stroke="currentColor"` so they inherit theme text color), each **paired with a
visible text label** for accessibility. The set: nav — Review=`square-pen`, Discover=`search`,
Profile=`user-round`, Track=`chart-column`; theme toggle=`sun`/`moon`; tour=`sparkles`. Sizing
classes: `.ic` (17px nav icon), `.btn-ic` (15px inline-button icon). Status still uses **dot +
label** (never color alone — WCAG 1.4.1, §5). Emoji may still appear incidentally in **content
copy** (e.g. `✓` on a done row), but chrome icons are the lucide SVG set, not emoji. Keep one
consistent icon style; don't scatter mixed glyphs.

### Layout behavior

ApplicationBot is desktop-first: a persistent left **sidebar nav** + a centered content column, in
both a browser tab and the native window. It is not a mobile-grid product — treat the Fluent
responsive *techniques* in §8 as the adaptation toolkit rather than a fixed breakpoint grid.

---

## 4. Color system

Canonical tokens from the `:root` block in [applicationbot/web.py](applicationbot/web.py). **Light**
values shown first, **dark** second. Dark tokens are **duplicated** in two blocks (media-query
default + explicit `[data-theme="dark"]` toggle) and must stay byte-identical — change both (see
[ui.md](ui.md)).

| Role | Token | Light | Dark | Use |
|------|-------|-------|------|-----|
| Accent fill / border | `--accent` | `#2563eb` | `#2563eb` | Buttons, focus rings, progress bars — paired with white text `--accent-ink` |
| Accent text | `--accent-text` | `#1d4ed8` | `#60a5fa` | Links, active-tab label, badges — `color:` only (see §5) |
| Accent ink (on fill) | `--accent-ink` | `#ffffff` | `#ffffff` | White text on an accent fill |
| Accent washes | `--accent-weak` / `--accent-weak-2` | `#eff6ff` / `#dbeafe` | `#182135` / `#20304d` | Soft accent backgrounds, chips |
| AI accent | `--ai` | `#7c3aed` | `#a78bfa` | AI/Claude-specific affordances only — distinct from the primary accent |
| Success | `--ok` (+ `-bg`, `-tint`) | `#16a34a` | `#22c55e` | Done / matched / saved — pair with label or ✓ |
| Danger | `--bad` (+ `-bg`) | `#c81e1e` | `#f87171` | Errors, destructive actions |
| Warning | `--warn` (+ `-strong`, `-line`, `-bg`, `-chip`) | `#a16207` | `#fbbf24` | Warnings, attention |

### Surfaces & text

| Role | Token | Light | Dark |
|------|-------|-------|------|
| Page background | `--bg` | `#fafafa` | `#09090b` |
| Card / nav surface | `--surface` | `#ffffff` | `#18181b` |
| Hover / pill surface | `--surface-2` | `#f4f4f5` | `#27272a` |
| Recessed input inset | `--field` | `#ffffff` | `#101013` |
| Body text | `--ink` | `#18181b` | `#ededef` |
| Strong text | `--strong` | `#09090b` | `#fafafa` |
| Secondary text | `--muted` | `#71717a` | `#a1a1aa` |
| Incidental text | `--faint` | `#a1a1aa` | `#71717a` |
| Line / border | `--line` | `#e4e4e7` | `#2e2e33` |

**Surface ladder** (elevation): `--bg` (page) → `--surface` (cards, nav) → `--surface-2` (hover,
pills). `--field` is a *recessed* inset for inputs/dropdowns — darker than its card in dark mode
(the "menus are darker" look).

**Palette philosophy (dark mode):** a shadcn "zinc" near-black — page `--bg:#09090b` (zinc-950),
cards `--surface:#18181b` (zinc-900), hover `--surface-2:#27272a` (zinc-800). Crisp, not soft:
structure comes from low-contrast **1px borders** and spacing, with shadow reduced to a hairline;
radius tightened to **10px** cards / **8px** controls. One restrained blue accent (`--accent`
blue-600 for fills, `--accent-text` for links/on-dark text). Light mode is shadcn light — zinc-50
page, white cards, zinc-200 borders. If asked to "soften," reach for spacing + border weight
before changing hues.

---

## 5. Color selection rules

### Hierarchy

1. **One primary action per view** — filled `--accent` with `--accent-ink` text.
2. **Secondary / structural** — quiet border on `--surface` (ghost).
3. **Tertiary** — `.linklike` / plain accent-text link.
4. **Destructive** — `--bad`; never style Cancel as prominently as the primary action.

### The two accent tokens — important

Accent blue is split into two tokens because a fill and text-on-dark need *opposite* lightness:

- **`color:` → `--accent-text`** (light enough to read on dark surfaces ≥ 4.5:1).
- **`background` / `border-color` → `--accent`** (dark enough that white text on it ≥ 4.5:1).

Do **not** use `--accent` as a text color on a dark background — it fails contrast.

### Semantic chips & status

Reuse the semantic token families; don't hardcode hex when a token exists:

| Meaning | Tokens |
|---------|--------|
| Info / in progress | `--accent-*` (blue tones) |
| Success / done / matched | `--ok`, `--ok-bg`, `--ok-tint` |
| Warning | `--warn`, `--warn-strong`, `--warn-line`, `--warn-bg`, `--warn-chip` |
| Danger / blocked | `--bad`, `--bad-bg` |
| Neutral | `--neutral-tint`, `--muted`, `--line` |
| AI / Claude | `--ai` (sparingly) |

### Contrast (WCAG 2.2 AA floor)

| Element | Minimum |
|---------|---------|
| Normal text (< 18.66px bold / < 24px) | **4.5:1** |
| Large text (≥ 18.66px bold or ≥ 24px) | **3:1** |
| UI component boundaries & focus indicators | **3:1** |

Practical ApplicationBot habits:

- Body: `--ink` / `--muted` on `--bg` or `--surface`.
- `--faint` is for **incidental** text only (placeholder-ish hints, decorative meta) and may sit
  slightly under 4.5:1 — **never put task-critical information in `--faint`**; use `--muted`.
- Dark theme: keep text bright (`--strong` ≈ white); don't use mid-gray body copy on a dark card.
- **Run the contrast checker** ([ui.md](ui.md) has the copy-paste Python) after any color change;
  current values all pass (dark white-on-accent 5.17; dark link 6.97; light button 5.17; light
  link 6.7).

### Never rely on color alone

Pair every status with **label, glyph, or pattern** (dot + text on chips, ✓ on done rows, an
icon/text on toasts) — WCAG 1.4.1.

### Disabled states

Lower opacity / desaturate intentionally, and — per [CLAUDE.md](CLAUDE.md) UI Principle #3 — explain
*what's blocking the user* rather than leaving a dead-looking control.

---

## 6. Typography

One system stack (see §3):

```css
font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
```

Guidelines:

- Headings/brand: weight **800**; UI emphasis / active tab: **600**; body: Regular. Line-height
  ~**1.2** headings, **1.4–1.6** body.
- Prefer `rem`/relative units so user zoom / OS text size works (WCAG 1.4.4 / 1.4.10).
- Tabular nums for IDs, counts, timestamps, token spend (`font-variant-numeric: tabular-nums` — the
  metric-tile sub-lines and token tables do this). Aligned numeric displays (count-pill numbers,
  metric-tile sub-lines, token tables, fit chart) and the live-run log stream also use the monospace
  stack `--mono` (see §3). A standalone big value — a metric-tile value or hero number — stays
  **proportional** sans (tabular is for columns, not display numbers).
- Uppercase, slightly-tracked (`letter-spacing:.03em`) only for **section meta labels** (form
  labels, table headers) — never body.
- Don't drop body below ~13px without a density rationale (dense tables run 11–13px meta).

Rough scale in use: brand ~15px/800, section headings ~15px, body ~14px, table/meta 11–13px.

---

## 7. Spacing, radius, elevation

### Spacing

Keep a consistent small-step rhythm (Fluent/M3 §2); avoid random `13px` paddings. Card/control
padding clusters around 8–18px; section gaps step up from there. Content sits in a centered column,
not edge-to-edge.

### Radius

| Use | Value |
|-----|-------|
| Cards, panels, controls container | `--radius` = **10px** |
| Inputs, buttons, tabs | **8px** |
| Small chips / reset buttons / table cells | 4–6px |
| Pills, status chips, dots | **99px** |

Cards and modals: `--radius`. Dense chips and filter pills: full (99px).

### Shadows

One **hairline** token — `--shadow` (`0 1px 2px rgba(0,0,0,.05)` in light; `0 1px 2px rgba(0,0,0,.4)`
in dark). The look leans on **1px borders**, not shadow elevation; don't stack glow + multiple
colored shadows.

---

## 8. Layout & composition

### App shell

- **Sidebar nav** + **centered content column** is the ApplicationBot pattern (Apple-style **UI
  layer** vs **content layer** — quiet chrome, expressive content).
- The same shell renders in a browser tab and the native window; never branch on the shell.
- Full-bleed only when a view is inherently wide (the tracker table scrolls horizontally in its own
  container); other pages stay in the centered column.

### Hierarchy per section

- One purpose, one headline, short supporting line.
- Primary action near the task; secondary actions quieter.
- Progressive disclosure for long flows (the setup checklist, collapsible profile cards) — show the
  next step, not everything at once.

### Spacing & proximity (Fluent)

- Elements close together read as related; extra space weakens the relationship and can signal
  importance. Prefer **whitespace over extra dividers** when grouping.

### Cards

- Use cards when the container is an **interactive unit** or a clear content panel (a tracker row
  detail, a profile card). Avoid card-wrapping everything.

### Responsive (Fluent techniques)

Desktop-first, but adapt gracefully:

| Technique | ApplicationBot example |
|-----------|------------------------|
| Reposition | Sidebar → top/collapsed on a narrow window |
| Resize | Tighten page pad; smaller controls when cramped |
| Reflow | Multi-column controls → stacked |
| Show/hide | Hide non-essential meta columns in the tracker on narrow widths |
| Re-architect | Wide table → its own horizontal-scroll container rather than squeezing |

- Pointer targets: **min 24×24 CSS px** (WCAG 2.2); go **~44px** for anything a phone user taps.
  Small action buttons (`padding:6px 13px`) clear the 24px floor.

---

## 9. Components

### Buttons

Separate **role** (hierarchy) from **state** (interaction) — per [Figma: button states](https://www.figma.com/resource-library/button-states/).

| Role | Treatment |
|------|-----------|
| Primary | Accent fill (`--accent`), white text (`--accent-ink`) |
| Secondary / ghost | Quiet border on `--surface` / `--surface-2` |
| Link-like | `.linklike` — accent-text, no fill |
| Danger | `--bad` text/fill for destructive (delete row, etc.) |

| Core state | Treatment |
|------------|-----------|
| Default | Clearly clickable at rest (contrast, shape, label) |
| Hover | Subtle `filter` darken / shadow lift — not a dramatic swap |
| Active / pressed | Brief darker fill or slight press |
| Focus (`:focus-visible`) | Global `2px solid var(--accent-text)` ring, `outline-offset:2px`; never `outline:none` without a replacement |
| Disabled | Muted opacity, `not-allowed`; explain *why* when possible |

| Functional state | Treatment |
|------------------|-----------|
| Loading | `btnBusy(btn, "Tailoring…")` — disable the trigger, spinner + **specific** label; `btnDone(btn)` restores. Never a dead, clickable-looking button. |
| Success | Short confirmation ("Saved ✓") — plain language |
| Error | Reset to clickable + an **actionable** inline message with the next step |
| Selected / pressed toggle | Persist until deselected (`aria-pressed`) — tabs, view toggles |

Rules: one primary per region; Cancel/ghost less prominent than the primary; map edge cases
(failure, stall, duplicate submit) before shipping a complex flow.

### Forms

Aligned with Nielsen Norman form guidance:

- Visible **labels** (uppercase meta labels), never placeholder-only.
- Group related fields; mark required clearly.
- Errors **inline next to the field**, plain language + how to fix.
- Prefer single-column for long forms; multi-step with progress for heavy onboarding.
- Avoid Reset/Clear; provide Cancel with lower visual weight.
- Focus: border → `--accent` + the global focus ring.

### Feedback

| Pattern | When |
|---------|------|
| Inline `.msg` / status line | Short confirmation / failure after an action |
| Banner / persistent status | Page-level state |
| Inline field error | Validation |
| Spinner + elapsed time | Loading; for anything > ~2s (a Claude call) show **elapsed seconds / real progress** so it never looks frozen |
| Empty state | Short next-step CTA, not a blank panel |

### Modals / dialogs

The **setup overlay is the reference implementation** — copy it for any new modal:
`role="dialog" aria-modal="true"` with `aria-labelledby`/`aria-describedby`; on open **move focus
in**; **trap Tab**; **Escape** and click-outside close; on close **restore focus** to the trigger.

### Tables & lists

- Sticky uppercase meta headers; hover row tint; tabular-nums for keys/counts (the tracker table).
- Wide tables scroll in their own container rather than squeezing the layout.
- Destructive bulk/row actions confirm before firing.

### Toggle switch

An **on/off** option renders as a switch (not a checkbox). Keep a real `<input type=checkbox>` so
JS reads `.checked`; style it with `appearance:none` + a `::after` thumb that slides on `:checked`
(track `--surface-2` → `--accent`). Reference rule: `.loop-rescan input[type=checkbox]`. Use
switches for on/off options; keep **plain checkboxes for multi-select** filter lists (e.g. seniority
levels).

### Metric tiles (KPI row)

Aggregate stats — the discovery→offer **funnel** and **token spend** — render as a row of bordered
stat cards (`.mtiles` grid of `.mtile`), the Apify/Bright Data "statistics" look, not bar rows.
Tile contract: **label** (uppercase meta), **value** (big — the tile value is **proportional sans
semibold**, *not* tabular; big numbers read loose in tabular-nums), an optional **mono sub-line**
(percent / exact count / conversion), and an optional **meter** (`.mtile-meter` — accent fill on an
`--accent-weak` track, the lighter step of the same ramp) that carries magnitude/drop-off. Build one
with the `mtile(label, value, sub, meterPct)` helper. The funnel keeps its drop-off reading via each
tile's meter width (relative to the top stage).

### Metrics as data

Numeric displays that sit in **columns or sub-lines** — count-pill numbers, tile sub-lines, token
tables, the fit chart, run-log timestamps — use `font-family:var(--mono)` + tabular-nums so figures
align and read as data (see §3, §6). A **standalone big value** (a tile/hero number) stays
proportional sans — reserve tabular for aligned columns.

### Live-run log

Live/technical status reads as a **recessed, monospace terminal-style stream** (background `--field`,
`font-family:var(--mono)`) — the Railway/Apify "live run log" look. Used by: the dry-run progress
panel (`.testprog`), the auto-apply **loop status** (`.loopstat`, with a colored `›` prompt glyph),
and the per-posting **run history** (`.runsbox`/`.runline` — dim timestamp, colored outcome tag,
muted detail, one log line per run). It still follows the feedback rules above (elapsed time / real
progress, definite end state). The reusable **`.terminal`** window (a `<code>`-style block: `--field`
background, `--mono`, `.tl` lines dim by default, `.tl.warn`/`.tl.err`/`.tl.ok` for levels, a
`.tl-prompt` `›`) is the shared building block — used in the context drawer's run log.

### Hero scorecards

A view leads with **3–4 scorecards** (`.scorecards` grid of `.scorecard`), never more — the
high-density-analytics rule. Each: identical 1px border, a **big proportional-sans value**
(`.sc-val`), a **smaller muted label** below (`.sc-label`) for immediate contrast hierarchy. Colour
the value only to flag a state (`.info` blue / `.warn2` amber / `.bad2` red); the default is
strong-ink. Track leads with Total processed / Applied / Blocked / Failed. Deeper breakdowns
(the funnel, token spend) go *below* or in a `<details>`, not in the hero row.

### Status system (badge + dot)

One muted status vocabulary, shared by the feed badge (`.stbadge` = dot + label) and the table
`st-*` cell colours, driven by `statusMeta()` in JS. Colour by **outcome, not decoration**:
neutral (`--muted`) = pending (discovered / tailored / dry-run / rejected / no-response), blue
(`--accent-text`) = applied, green (`--ok-text`) = positive reply (responded / interview / offer),
amber (`--warn`) = blocked/needs-you, red (`--bad`) = failed. No oversized success ticks — a small
tag. A genuinely **live/active** state adds `.live` to pulse the dot; a terminal outcome never pulses.
Always dot **+ label** (never colour alone — WCAG 1.4.1).

### Card feed & metadata strings

A scannable list of records is a **feed of uniform cards** (`.fcard`): a bold title
(company · role), a tight **metadata string** — borderless, `•`-separated, mono (`greenhouse •
West Athens, CA • 2 runs`, via `metaString()`), never a table of labels — plus a trailing metric and
status badge. Offer a **Feed | Table segmented toggle** (`.viewtog`) when a denser editable table
must remain available; remember the choice per browser. The whole card is the click target.

### Context drawer

Clicking a feed card slides in a right **drawer** (`.drawer` + `.drawer-scrim`) with the record's
detail — status, `•`-meta, action buttons, and its run-log `.terminal`. It is an ARIA dialog: move
focus in, **trap Tab**, close on Esc / X / scrim click, restore focus on close (same contract as the
setup overlay). Prefer a drawer over navigating away when the detail is *context for the list*.

### Settings modal

Heavy configuration that isn't part of the primary flow (Discovery settings) lives in a centered
**modal** (`.modal-scrim` + `.modal`), opened from a labeled button, so the working page stays to
actions + status. Same dialog contract (Esc / X / scrim close, focus restore). Keep the form's ids
stable when relocating it into the modal so render/save wiring is unchanged.

### Aligned panel headers

An action panel puts its **primary button on the same line as its title** (`.panel-head`: title
flex-1 left, button right), with a 1–2 line description below — never a wall of text with the button
buried underneath. Trim `.editing` copy to `.editing.tight`; move detail to a hint or tooltip.

---

## 10. Interaction & usability heuristics

Apply [Nielsen's 10 heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/):

1. **Visibility of status** — tailoring progress, saved drafts, run history, token spend; never
   leave the user in silence ([CLAUDE.md](CLAUDE.md) UI Principle #5).
2. **Speak the user's language** — job-application terms (posting, tailor, apply, tracker), not
   internal jargon, on user-facing copy.
3. **User control** — Cancel, Escape, click-out; dry-run by default so a submit is never a surprise.
4. **Consistency** — same tokens, one button style, one sidebar, one modal pattern everywhere.
5. **Error prevention** — confirm destructive deletes; arm real submission deliberately
   (Agent Guideline #3), constrain pickers vs free-text where possible.
6. **Recognition over recall** — persistent nav, visible field labels, the setup checklist showing
   what's left.
7. **Flexibility** — keyboard reachable; dense table view for scanning, spacious view for onboarding.
8. **Minimalism** — strip chrome that doesn't serve the current task; content over decoration.
9. **Error recovery** — plain-language messages + the next action, ideally a one-click deep-link to
   the exact field/step ([CLAUDE.md](CLAUDE.md) UI Principle #2; see the checklist's `action`
   deep-links and `goToProfileAnswers()`).
10. **Help in context** — short inline hints, not a wall of upfront tutorial.

### Motion

- Fast transitions (~120–200ms); ease-out for entrances.
- Honor `prefers-reduced-motion: reduce` (disables the decorative flash pulse and smooth-scroll) —
  but **keep functional spinners running**; a frozen "working…" indicator is worse than motion.
- Motion for hierarchy/feedback, not decoration.

---

## 11. Accessibility checklist

- [ ] Text and meaningful glyphs meet WCAG 2.2 AA contrast (run the checker in [ui.md](ui.md))
- [ ] Focus visible via `:focus-visible` (never removed without a replacement)
- [ ] Status not color-only — pair with label / ✓ / dot (WCAG 1.4.1)
- [ ] Forms: associated labels; errors inline + linked
- [ ] Dialogs: focus moved in, Tab trapped, Escape/click-out closes, focus restored (setup overlay)
- [ ] Keyboard: all actions reachable; Tab order matches reading order
- [ ] Targets ≥ 24×24px (prefer 44×44 for anything a phone taps)
- [ ] `prefers-reduced-motion` honored; functional spinners still run
- [ ] Content usable at 200% zoom / reflow
- [ ] Both **dark** and **light** verified (dark tokens duplicated in both blocks — [ui.md](ui.md))

---

## 12. Theming (light / dark)

- The UI ships **both** light and dark. Dark tokens live in **two** blocks — the
  `@media (prefers-color-scheme: dark)` default and the explicit `:root[data-theme="dark"]` toggle —
  and must stay **byte-identical**; any dark-token change goes in both (a single `replace_all` edit
  updates both). See [ui.md](ui.md).
- Override the **same token names** per theme — don't fork every component.
- The native window respects `prefers-color-scheme`, so the toggle and OS default must agree.
- Never hardcode a light fill on a link/chip that must also read in dark.

---

## 13. Implementation conventions

### Token hierarchy (Figma / W3C-style)

| Layer | What it is | ApplicationBot examples |
|-------|------------|-------------------------|
| **Primitive** | Raw value | the hex on the right of a `--token:` in `:root` |
| **Semantic** | Role / intent | `--accent`, `--accent-text`, `--ink`, `--muted`, `--line`, `--ok`, `--bad`, `--warn` |
| **Component** | Bound to a control | Prefer semantic in CSS; only add a component token when a control must diverge |

Rules from Figma's token guidance:

- Name for **what it does**, not what it looks like (`--muted`, not `--gray`).
- Themes swap **semantic** values; keep class names stable.
- One source of truth — don't leave the same blue as a hardcoded hex in one rule and a `--token`
  in another.

### Engineering habits

1. **Add or extend a token** in the `:root` block before scattering hex in a rule.
2. Prefer existing classes (`.tab`, `.pill`, `.msg`, `.linklike`, table primitives) over one-off
   style blocks.
3. Feature styles may refine layout but should **consume tokens**.
4. Keep helpers centralized — `el(tag, props, kids)` builds DOM, `$(id)` is `getElementById`,
   `btnBusy`/`btnDone` are the one spinner pattern. Don't reinvent per feature.
5. When a durable visual rule is decided, update **this doc** and [ui.md](ui.md).

### Where to edit

| Concern | Location |
|---------|----------|
| Tokens, all CSS, all JS | The embedded string in [applicationbot/web.py](applicationbot/web.py) (`:root`, `<style>`, `<script>`) |
| Native window behavior | [applicationbot/app.py](applicationbot/app.py) |
| Setup checklist steps | `_setup_status()` in web.py + a doctor check in [applicationbot/doctor.py](applicationbot/doctor.py) |
| Rules & rationale | [CLAUDE.md](CLAUDE.md) UI Design Principles |

### How to verify (drive the real thing — don't eyeball only)

Per the repo's "shipped over tested" bias:

1. `./scripts/dev.sh` while iterating — the server restarts on save and the page reloads itself.
2. Screenshot with Playwright (already a dependency) in **both** `color_scheme="dark"` and
   `"light"`; stub endpoints with `page.route(...)` for states you don't have data for.
3. Assert interactions (focus moved into a dialog, Tab trapped, skip persists).
4. Run the contrast checker for any new color.

---

## 14. Quick do / don't

**Do**

- Use shadcn zinc neutrals + one restrained blue accent as the spine
- Put contrast first when users report "hard to see"; run the checker
- One primary action per view; quiet Cancel
- Use the one system font stack; add tokens before hex
- Prefer whitespace grouping over extra divider lines
- Design button roles *and* states (incl. loading / error recovery) — reuse `btnBusy`/`btnDone`
- Keep nav chrome quiet; put accent energy in content/actions
- Change **both** dark-token blocks together; verify light and dark

**Don't**

- Invent a third accent per screen (`--ai` is the only secondary accent)
- Placeholder-only form labels
- Remove focus outlines
- Purple/glow/cream-serif defaults for the product UI
- Use `--accent` as text on a dark background, or task-critical text in `--faint`
- Rely on color/emoji alone for status
- Write shell-specific UI (must work in browser tab and native window)

---

## References

### Platform & design systems

- Apple — [Human Interface Guidelines · Design principles](https://developer.apple.com/design/human-interface-guidelines/design-principles) · [WWDC26: Principles of great design](https://developer.apple.com/videos/play/wwdc2026/250/)
- Figma — [Design Systems 102](https://www.figma.com/blog/design-systems-102-how-to-build-your-design-system/) · [Components, styles & shared libraries](https://www.figma.com/best-practices/components-styles-and-shared-libraries/) · [Design tokens](https://www.figma.com/resource-library/design-tokens/) · [Button states](https://www.figma.com/resource-library/button-states/)
- Google — [Material Design 3](https://m3.material.io/) (color roles, type roles, theming)
- Microsoft — [Fluent 2 · Layout](https://fluent2.microsoft.design/layout) (spacing, proximity, responsive techniques)

### Usability & accessibility

- Nielsen Norman Group — [10 Usability Heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/) · [Web Form Design](https://www.nngroup.com/articles/web-form-design/)
- W3C — [WCAG 2.2](https://www.w3.org/TR/WCAG22/) (AA contrast, target size, focus appearance)

### ApplicationBot internal

- [CLAUDE.md](CLAUDE.md) — UI Design Principles + AI Coding Guidelines (the rules)
- [ui.md](ui.md) — how the UI is built, the token reference, the contrast checker, verification steps
- [applicationbot/web.py](applicationbot/web.py) — the tokens, CSS, and JS themselves

---

*Last updated from the shipped UI in [applicationbot/web.py](applicationbot/web.py) and [ui.md](ui.md),
plus Apple HIG / Figma / Material / Fluent / NN/g / WCAG guidance (2026).*
