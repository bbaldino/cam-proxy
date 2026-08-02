# Theming `webrtc-doorbell.html`

`webrtc-doorbell.html` is embedded cross-origin in an iframe by other pages
(dashboards, Home Assistant). It accepts CSS from its embedder over
`postMessage` and applies it live, with no reload. This document is the
contract for anyone building a parent page against it. `test-embed.html` in
this repo is a working reference parent — copy the handshake from it directly
rather than re-deriving it from this document.

`test-embed.html` is a development tool, not a production surface: it is not
shipped in the container image, and it is served from the same origin as the
doorbell page itself. To see its buttons do anything, add that serving origin
(e.g. `http://localhost:8899`) to `DOORBELL_THEME_ORIGINS` — otherwise every
payload it sends is correctly rejected by the allowlist, and the harness
looks broken when it is actually working as designed.

## Protocol

Page → parent:

```js
{ type: 'doorbell:ready', contract: 1 }   // page can accept styling
{ type: 'doorbell:video-playing' }        // first frame rendered
```

Parent → page, any number of times:

```js
{ type: 'doorbell:style', css: '...' }
```

The page injects `css` verbatim into a single `<style id="doorbell-override">`
appended last in `<head>`, replacing its previous contents on every message.
Because the override sheet is last in document order, it wins ties against
any base rule of equal specificity — but not every base rule sits at the same
specificity.

Every base rule that sets a **resting** appearance is a single
class/attribute selector — specificity `(0,1,0)` — so the injected sheet,
sitting at the same specificity, always wins there without needing
`!important` or a specificity fight. Seven base rules that set a **state**
appearance are two-attribute compounds — `(0,2,0)` — and these outrank a bare
`(0,1,0)` override *while that state is active*:

- `[data-doorbell="debug"][data-visible]`
- `[data-doorbell="reply"][data-doorbell-playing]`
- `[data-doorbell="talk"][data-doorbell-talking]`
- `[data-doorbell="talk"][data-doorbell-mic="denied"]`
- `[data-doorbell-talk="off"] [data-doorbell="talk"]`
- `[data-doorbell-state="live"] [data-doorbell="overlay"]` (fading the loading
  overlay out — see the containment tree for the removal that follows it)
- the two `[data-doorbell-reply-count="0"]` rules (hiding the heading and the
  replies container)

These compounds are deliberate — each is commented in the base sheet
explaining why — not oversights to be fixed. The reliable way to restyle one
of these states is either to override the CSS variable it reads (e.g.
`--doorbell-success`, which both the "reply playing" and "talking" colours
come from), or to write a selector of the same two-attribute specificity
yourself, e.g. `[data-doorbell="talk"][data-doorbell-talking]{background:blue}`.
A plain `[data-doorbell="talk"]{background:blue}` override will apply at rest
but silently lose to the base rule the moment that state becomes active.

### `doorbell:ready` fires on every load — theme on every one, not just the first

`doorbell:ready` is a top-level statement in the page's script, so it fires on
**every** initialisation — first load, refresh, `src` change, or
cache-busting `?v=` bump alike. The page contains no self-reload path
(`location.reload`, `location.href`, and `location.replace` appear nowhere in
it), so a reload only ever happens because the embedder caused one.

Therefore: **send your CSS in response to every `doorbell:ready`, not just the
first.** A parent that themes only once will come back unthemed after any
reload (e.g. a dashboard that mounts a fresh iframe on every doorbell ring),
and nothing will report the failure — the page simply renders with defaults
again, silently.

There is also a startup race to guard against: if a parent attaches its
`message` listener after the iframe's script has already run, it can miss
`doorbell:ready` entirely — the message fires once, synchronously in page
script, and is not replayed for a listener that shows up late. As a
belt-and-braces measure, parents should also send their CSS on the iframe
element's own `load` event, in addition to responding to `doorbell:ready`,
so a themed reload doesn't depend on listener attach order.

### Contract versioning

The contract version travels in the `doorbell:ready` message, not in the DOM.
The embed is cross-origin, so a `data-doorbell-contract` attribute mirrored
onto the root element is unreadable by the parent and cannot drive its
fallback logic — it exists only for local debugging inside the iframe. On an
unrecognised `contract` value, send variables only and skip layout CSS:
variables degrade gracefully, layout CSS does not.

## Origin allowlist

The server injects an allowlist of trusted parent origins into the served
page (`<meta name="doorbell-theme-origins">`), configured via the
`DOORBELL_THEME_ORIGINS` environment variable (see `README.md`). Messages
from any other origin are ignored, and the page logs `console.warn` when this
happens — that warning is the diagnostic tool, since the embedder cannot
otherwise inspect a cross-origin iframe to find out why theming isn't
applying.

### Rule: origins must be https

**Every allowlisted origin must be `https://`, or `http://localhost` /
`http://127.0.0.1`.** WebRTC and `getUserMedia` require a secure context, and
a document is only a secure context if its own origin is trustworthy *and*
every ancestor is. An https doorbell page inside a plain-http parent is
therefore not a secure context at all — video will not connect and the talk
button cannot work, regardless of theming. The server warns at startup for
any configured origin that fails this check. This isn't a theming-specific
policy; it's a precondition for the page working at all when framed.

### Operator note: origins are normalised for you

`DOORBELL_THEME_ORIGINS` entries are lowercased and have a default port
(`:443` for `https`, `:80` for `http`) stripped before matching, so configure
origins however is convenient — mixed case and an explicit default port both
still match at runtime.

### Operator note: two different warnings mean two different problems

- **"no theming origins configured"** — logged once, right before
  `doorbell:ready` fires, when the allowlist arrives at the page empty. This
  fires either because `DOORBELL_THEME_ORIGINS` is genuinely unset/empty, or
  because the page was served by a route that bypassed allowlist injection
  entirely (e.g. a URL variant that fell through to the plain static file
  handler instead of the route that injects the meta tag). If you see this,
  double-check the exact URL the iframe is loading reaches the injection
  route, not a lookalike path.
- **"ignoring doorbell:style from non-allowlisted origin: `<origin>`"** —
  logged per rejected message, when the allowlist is non-empty but does not
  contain the sender's actual origin. If you see this, the injection route is
  working; the configured origin string doesn't match the parent's real
  origin (case and a default port are normalised automatically — see above —
  so look instead for a scheme mismatch (`http` vs `https`) or a non-default
  port that doesn't match the parent's actual origin).

The first is "theming does nothing, and never will until configuration is
fixed"; the second is "theming does nothing for this specific parent."

## The containment tree

Hooks are `data-*` attributes, not classes or ids — they read as API rather
than as the page's own styling. Layout is inherently about containment
(`flex-direction` on a container, sizing on its children), so **every element
with a labelled descendant is itself labelled** — there are no unreachable
wrapper elements on the path between any two hooks:

```
[root]                      <body>
├── [overlay]               loading overlay
│   └── [overlay-inner]     centering wrapper
│       ├── [spinner]
│       └── [overlay-text]  "Connecting to doorbell..."
└── [layout]                the flex container
    ├── [stage]             wrapper around the video
    │   ├── [debug]         stats overlay
    │   └── [video]         the <video> element
    ├── [sidebar]           the panel
    │   ├── [replies-heading]
    │   ├── [replies]       quick-reply container
    │   │   └── [reply] ×N  each reply button
    │   └── [controls]      talk/mute container
    │       ├── [mute]
    │       └── [talk]
    └── [debug-toggle]      the "i" button
```

`[overlay]`, `[overlay-inner]`, `[spinner]`, and `[overlay-text]` exist only
until the first frame plays: 300ms after the page reaches `data-doorbell-state="live"`,
the script calls `overlay.remove()` and the whole subtree is removed from the
DOM, not just hidden. Styling these four for a state that occurs later (e.g.
a mid-stream `error`) has no effect, because by then the element is gone.

Each name above is the value of a `data-doorbell` attribute, e.g.
`[data-doorbell="stage"]` selects the video wrapper. `replies` and `controls`
are deliberately separate containers so quick replies and the talk/mute
controls can be positioned independently of each other — see harness payload
4 (`test-embed.html`) for a working example that puts them in different parts
of the layout.

## Variables

**This table is definitive.** Every default below is copied directly from the
`:root` block in `webrtc-doorbell.html`. These are not fallback values for
some richer "real" appearance — the Chromecast display and the Home
Assistant card receive no `doorbell:style` message at all, so these defaults
are literally and permanently what those two surfaces render. Build against
this table without needing to reconstruct it by reading the page's CSS.

| Variable | Default | Used for |
|---|---|---|
| `--doorbell-bg` | `#d0d0d0` | page background, loading overlay |
| `--doorbell-surface` | `#c0c0c0` | sidebar background |
| `--doorbell-text` | `#444` | headings |
| `--doorbell-text-muted` | `#555` | overlay text, mute button |
| `--doorbell-accent` | `#3a7bd5` | quick-reply buttons, spinner accent |
| `--doorbell-accent-text` | `#fff` | text on accent-coloured elements |
| `--doorbell-danger` | `#c0392b` | talk button, idle state |
| `--doorbell-success` | `#27ae60` | talk button active, reply playing |
| `--doorbell-border` | `rgba(0, 0, 0, 0.1)` | button borders, spinner track |
| `--doorbell-font-body` | `sans-serif` | everything except the two rows below |
| `--doorbell-font-display` | `sans-serif` | headings |
| `--doorbell-font-mono` | `monospace` | stats/debug overlay, numerals |

Hover and "active" shades (e.g. the reply button hover state) are derived from
these via `color-mix()` in the base sheet rather than exposed as separate
variables.

**Fonts are variables, not inheritance.** Setting `font-family` on the root
and relying on cascade is fragile — any descendant rule that happens to set
`font-family` silently wins over inheritance, invisibly to the embedder. The
base sheet sets `font-family` in exactly four places: root, headings, the
mono/stats overlay, and the debug-toggle button (the "i" in the corner). The
first three match the three variables above; the toggle is a `<button>`, and
form controls do not inherit `font-family` from an ancestor, so it needs its
own declaration — it reuses `--doorbell-font-mono` rather than introducing a
fourth variable. No other rule in the base sheet sets `font-family`.

Font *files* still come from the embedder via `@font-face` in the same CSS
payload. Note: injected CSS referencing `http://` subresources (including
fonts) is blocked as mixed content by the browser once the page is served
over https, so font URLs must be https too.

**Variables are primary; layout CSS is the escape hatch.** Prefer the
variable list above for anything colour-related. Reach for raw layout CSS
against the containment tree only when repositioning, not recolouring — that
way a future restructure of this page can cost you positioning but never
legibility.

## State hooks

| Hook | Element | Meaning |
|---|---|---|
| `data-doorbell-state="loading\|connecting\|live\|error"` | root | connection lifecycle |
| `data-doorbell-talk="on\|off"` | root | whether `?talk=0` hid the talk button |
| `data-doorbell-reply-count="N"` | `[replies]` **and** root | number of replies currently loaded |
| `data-doorbell-talking` | `[talk]` | present while transmitting |
| `data-doorbell-muted` | `[mute]` | present while muted |
| `data-doorbell-mic="denied"` | `[talk]` | mic permission refused |
| `data-doorbell-playing` | `[reply]` | present while that specific reply plays |

These are hooks, not built-in styling — the base sheet publishes nearly all of
them without applying a look, because the defaults are what the Chromecast
display and the HA card render today and neither has asked for a different
one. Style them from your own CSS as needed.

There is no separate error-message element: a denied mic permission shows up
as the talk button's own label change, hooked by `data-doorbell-mic="denied"`.
Style the talk button for that state rather than expecting text elsewhere.

## Rules that bite

Four rules are easy to violate by accident. All four are load-bearing.

1. **Allowlisted origins must be https** (or `http://localhost`). See
   *Origin allowlist* above — this isn't optional hardening, it's what makes
   the iframe a secure context at all.

2. **`data-doorbell-reply="<slug>"` is cosmetic only, never load-bearing.**
   Slugs are user-editable through the doorbell's admin UI, and any reply can
   be deleted at any time. CSS that targets a specific slug will silently stop
   applying the moment that reply is renamed or removed. Use it for
   presentation only (e.g. a debugging label), never for layout your page
   depends on.

3. **Layout must never assume a reply count.** Quick replies are
   user-configured and the set can be empty, one, or many, and it changes
   after first paint (the list is fetched asynchronously and starts empty).
   No CSS on either side may use a fixed height, structural `nth-child`
   positioning, or a sidebar width derived from the number of buttons.
   `data-doorbell-reply-count` (mirrored onto both `[replies]` and root) is
   there so you can vary layout *by* count, without ever hard-coding one.

4. **State rules outrank a plain override while that state is active.** As
   covered under *Protocol* above, six base rules that express a state are
   two-attribute compounds, which are more specific than a bare one-attribute
   override — this is a class of gotcha, not a single one. The
   reply-count-zero case is the worked example: the base sheet hides the
   "Quick Replies" heading and the replies container when the count is zero,
   via:

   ```css
   [data-doorbell-reply-count="0"] [data-doorbell="replies-heading"],
   [data-doorbell-reply-count="0"] [data-doorbell="replies"] { display: none; }
   ```

   This is an attribute-selector rule scoped to the zero-count ancestor, which
   is more specific than a bare `[data-doorbell="replies-heading"] { display:
   ... }` override. If your CSS sets `display` on `replies-heading` or
   `replies` for its own layout purposes, it will win over the base rule at
   *non-zero* counts but **lose** to it at zero — which is what you want,
   except you must not assume your override alone controls visibility. If you
   need the heading to behave differently at zero replies than this default
   (hidden), you must write your own `[data-doorbell-reply-count="0"]`-scoped
   rule; a plain override of `replies-heading` will not reach that case.

   The same shape applies to the other six state rules: a plain override
   changes the resting look but is silently beaten the instant the state rule
   applies. Match the compound's specificity, or restyle via the CSS variable
   the state rule reads, rather than assuming a bare override reaches every
   state.

   The loading overlay is the subtlest of them, because it has two ways to
   defeat you rather than one: while the page is `live` the base rule holds it
   at `opacity: 0` against a bare override, and 300ms later the element is
   removed from the DOM entirely, at which point no selector of any specificity
   reaches it. Style the overlay for `loading`, `connecting`, and `error`
   states that occur *before* the first frame plays; anything you write for
   after it has nothing to apply to.

## What's out of scope here

- **Font delivery** depends on the embedder's own hosting (stable URLs plus
  CORS, if fonts are cross-origin from the doorbell page's perspective).
  Until an embedder's font infrastructure is in place, `@font-face` payloads
  will silently fall back to the generic family requested. This is why
  `test-embed.html`'s font demo payload uses only generic families
  (`Georgia, serif` / `"Courier New", monospace`) — it doesn't depend on any
  external asset.
- **Home Assistant's `custom:doorbell-card` embed is not themed today.** Its
  origin is not in the default allowlist, so it renders the defaults above
  unmodified. This is a recorded decision, not an oversight.
