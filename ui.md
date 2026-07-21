# ui.md — UI reference for ApplicationBot

The single source of truth for how ApplicationBot's interface looks and behaves. Read this
before changing anything user-facing. It complements — does not replace — the **UI Design
Principles** and **AI Coding Guidelines** in [CLAUDE.md](CLAUDE.md); those are the rules, this
is the how.

Grounded in Apple's [Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines)
(clarity, deference, depth; consistency; feedback; forgiveness) and the
[WCAG 2.1 AA](https://www.w3.org/WAI/WCAG21/quickref/) success criteria.

---

## Where the UI lives

The **entire** interface — HTML, CSS, and JS — is embedded as one big string in
[applicationbot/web.py](applicationbot/web.py) (a stdlib `http.server` app, no framework, no
build step). There is no separate CSS/JS file. To change the look, edit the `<style>` block; to
change behavior, edit the inline `<script>`. Server endpoints return JSON via `self._json(...)`.

The same UI renders in two shells: a **browser tab** (`applicationbot.web`) and a **native desktop
window** (`applicationbot.app`, a pywebview WKWebView/WebView2 window). You never write shell-specific
UI — style for a standard webview and it works in both. The window respects `prefers-color-scheme`,
so the theme tokens and toggle behave exactly as in the browser.

Handy anchors when editing:
- `:root { … }` — light design tokens (CSS variables).
- `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) … }` **and**
  `:root[data-theme="dark"] { … }` — the dark tokens, **duplicated** (media-query default +
  explicit toggle). **Any dark-token change must be made in BOTH blocks** or the theme toggle and
  the OS default will disagree. They are byte-identical today; keep them so (a single
  `replace_all` edit updates both).
- The `el(tag, props, kids)` helper builds DOM (`{class, text, value, on:{event:fn}}`); `$(id)`
  is `getElementById`.

---

## Design tokens (CSS variables)

Never hard-code a hex in a rule — use a token so light/dark and future retints stay consistent.
Current families: surfaces (`--bg`, `--surface`, `--surface-2`, `--field`), text (`--ink`,
`--strong`, `--muted`, `--faint`), lines (`--line`), accent (see below), status (`--ok/--bad/
--warn` + their `-bg/-tint/-chip`), shape (`--radius`, `--shadow`), and a monospace data font
(`--mono`, theme-independent — for metrics/data and the live-run log).

**Surface ladder** (dark → light elevation): `--bg` (page) → `--surface` (cards, nav) →
`--surface-2` (hover, pills). `--field` is a *recessed* inset for inputs/dropdowns (darker than
its card in dark mode — the "menus are darker" inset look).

**Palette philosophy (dark mode):** a shadcn "zinc" near-black — page `--bg:#09090b` (zinc-950),
cards `--surface:#18181b` (zinc-900), hover `--surface-2:#27272a` (zinc-800). It is crisp, not
soft: structure comes from low-contrast **1px borders** and spacing, with shadow reduced to a
hairline; radius is tightened to **10px** cards / **8px** controls. One restrained blue accent
(`--accent` blue-600 for fills, `--accent-text` for links/on-dark text). Light mode is shadcn
light — zinc-50 page, white cards, zinc-200 borders. If asked to "soften," reach for spacing +
border weight before changing hues.

### The two accent tokens — important

Accent blue is split into **two** tokens because a fill and text-on-dark need *opposite*
lightness to stay legible:

| Token | Use it for | Rule of thumb |
|---|---|---|
| `--accent` | **Fills & borders**: `background:var(--accent)`, `border-color:var(--accent)`, focus rings, progress bars. Paired with white text (`--accent-ink`). | Must be dark enough that **white text on it ≥ 4.5:1**. |
| `--accent-text` | **Accent-colored text**: `color:var(--accent-text)` — links, active-tab label, badges, `.linklike`, `.pnav a`. | Must be light enough to read **on dark surfaces ≥ 4.5:1**. |

So: **`color:` → `--accent-text`; `background`/`border-color` → `--accent`.** Do not use
`--accent` as a text color on a dark background — it will fail contrast.

---

## Accessibility requirements (WCAG AA)

These are hard requirements, verified — not aspirations.

1. **Contrast.** Text ≥ **4.5:1** (normal) or **3:1** (large: ≥ 18.66px bold / 24px). UI
   component boundaries and focus indicators ≥ **3:1**. Run the checker below after any color
   change; every text/background pair the user must read has to pass.
   - `--faint` is the one exception: it is for **incidental** text only (placeholder-ish hints,
     decorative meta) and may sit slightly under 4.5:1. **Never put information the user needs to
     complete a task in `--faint`** — use `--muted` (which passes) for that.
2. **Don't rely on color alone** (WCAG 1.4.1). Pair color with a glyph/text: e.g. the setup
   checklist uses a **✓** on done rows, not just green; status uses labels, not only hue.
3. **Visible keyboard focus** (2.4.7). A global `:focus-visible { outline:2px solid
   var(--accent-text); outline-offset:2px }` covers buttons, tabs, links. `:focus-visible`
   (not `:focus`) means it shows for keyboard users but not on mouse click. Never remove outlines
   without replacing them.
4. **Respect reduced motion** (2.3.3). `@media (prefers-reduced-motion: reduce)` disables the
   decorative flash pulse and smooth-scroll. **Keep functional spinners running** — a frozen
   "working…" indicator is worse than motion. JS scrolls check `matchMedia('(prefers-reduced-
   motion: reduce)')` and pass `behavior:"auto"` when set.
5. **Dialogs** (ARIA dialog pattern). The setup overlay is the reference implementation:
   `role="dialog" aria-modal="true"` with `aria-labelledby`/`aria-describedby`; on open, **move
   focus into** the dialog; **trap Tab** inside it; **Escape** and click-outside close it; on
   close, **restore focus** to the trigger. Copy this pattern for any new modal.
6. **Target size.** WCAG 2.5.8 floor is **24×24 px**; HIG's touch ideal is 44pt. This is a
   desktop web app, so 24px is the bar — small action buttons (`padding:6px 13px`) clear it. Go
   bigger for anything a phone user taps.

### Contrast checker (run before committing a color change)

```python
def lin(c):
    c/=255; return c/12.92 if c<=0.03928 else ((c+0.055)/1.055)**2.4
def L(h):
    h=h.lstrip('#'); r,g,b=int(h[0:2],16),int(h[2:4],16),int(h[4:6],16)
    return 0.2126*lin(r)+0.7152*lin(g)+0.0722*lin(b)
def ratio(fg,bg):
    a,b=L(fg)+0.05,L(bg)+0.05; return round(max(a,b)/min(a,b),2)
# e.g. white on the dark button, then the active-tab link:
print(ratio('#ffffff','#2563eb'), ratio('#60a5fa','#18181b'))   # want >= 4.5
```

Current values all pass AA (dark button white-on-accent 5.17; dark link 6.97; light button
5.17; light link 6.7).

---

## Feedback & waiting (never leave the user in silence)

Reuse the **shared** pattern (CLAUDE.md UI Principle #5) — do not invent a new spinner/toast per
feature:
- `btnBusy(btn, "Tailoring…")` disables the trigger and shows a spinner + specific label;
  `btnDone(btn)` restores it.
- Show in-place status where the result will appear; for anything > ~2s (a Claude call), show
  **elapsed seconds / real progress** so it never looks frozen.
- End in a definite state: the result, a "Saved ✓" marker, or an **actionable** inline error that
  says what failed and how to fix it (Principle #3) — and, where possible, a one-click deep-link
  to the exact field/step (Principle #2; see `goToProfileAnswers()` and the setup checklist's
  `action` deep-links as references).
- If the system drops/skips/truncates user input, **say so** — silence reads as "ignored me."

---

## First-run walkthrough (the onboarding checklist)

A skippable overlay (`#setup-overlay`) that reads `GET /setup/status` — which reuses
[doctor.py](applicationbot/doctor.py)'s readiness checks — and renders one row per setup step.
Each unfinished row has **one** button that deep-links to where it's fixed (tab switch + scroll +
`flash`), honoring Principle #2. It auto-opens on a fresh, unfinished clone unless the user
skipped (persisted in `localStorage["ab-setup-skipped"]`); reopen from the nav's "Take the tour"
button (a lucide `sparkles` SVG icon + label — chrome icons are lucide inline SVGs, not emoji). To add a step: extend `_setup_status()` in web.py (reuse a doctor check for
ok/detail/fix; add a UI `action`) and, if needed, a CTA label in the JS `CTA` map.

---

## How to verify UI changes (do this — don't eyeball only)

Per the repo's "shipped over tested" bias, **drive the real thing**:
0. While iterating, run **`./scripts/dev.sh`** (= `run.sh --dev`): the server restarts on every
   save and the open page reloads itself, so UI edits show immediately without a manual restart
   (the UI string is built at import, so a restart *is* required — the reloader just automates it).
1. Start it: `./scripts/run.sh <port>` (or `python -m applicationbot.web --port <port>`).
2. Screenshot with Playwright (already a dependency) in **both** `color_scheme="dark"` and
   `"light"`; for states that need data you don't have, stub the endpoint with `page.route(...)`
   (the walkthrough's incomplete state is tested this way).
3. For interactions, assert behavior (focus moved into a dialog, Tab stays trapped, skip
   persists) — see the walkthrough tests used during its build.
4. Run the contrast checker for any new color.

---

## HIG takeaways worth keeping in mind

- **Clarity & deference** — content first; chrome recedes. The neutral gray theme and restrained
  accent are deliberate; don't add decoration that competes with the résumé/job content.
- **Consistency** — same control, same look and behavior everywhere. One spinner, one button
  style, one modal pattern.
- **Feedback** — every action acknowledges itself immediately (see above).
- **Forgiveness** — dry-run by default, skippable onboarding, Escape/click-out to dismiss,
  clearly-labeled destructive actions. Make the safe path the easy one.
- **Progressive disclosure** — show the next step, not everything at once (the checklist, the
  collapsible profile cards). Don't overwhelm a first-run user.
