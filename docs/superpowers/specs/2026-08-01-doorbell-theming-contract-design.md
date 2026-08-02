# Doorbell Page Theming Contract — Design

**Date:** 2026-08-01
**Component:** cam-proxy (`webrtc-doorbell.html`, `server2.py`)
**Status:** Approved design, pending implementation plan

## Problem

`webrtc-doorbell.html` is embedded in an iframe by the custom dashboard
(`/home/bbaldino/work/dashboard`, served at `https://dashboard.baldino.me`) and by
Home Assistant. The page hardcodes every colour it uses:

| Element | Colour | Location |
|---|---|---|
| page + loading overlay bg | `#d0d0d0` | `webrtc-doorbell.html:9,40` |
| sidebar | `#c0c0c0` | `:23` |
| headings | `#444` | `:25` |
| quick-reply buttons | `#3a7bd5` (hover `#4a8be5`) | `:27,30` |
| reply playing | `#2e8b57` | `:31` |
| talk idle / active | `#c0392b` / `#27ae60` | `:36,37` |
| mute | `#555` | `:38` |
| spinner accent, overlay text | `#3b82f6`, `#555` | `:47-51` (inline attrs) |

The result is a fixed light-grey page sitting inside dashboards with entirely
different palettes. An iframe cannot inherit the parent's CSS, and the embed is
cross-origin, so the parent cannot reach into the document either.

### Secondary defect this fixes

The dashboard's ring popup (`DoorbellRingModal.tsx`) mounts a *fresh* iframe on
every doorbell press, over a black/90 scrim. The loading overlay is `#d0d0d0`
with an inline-styled spinner, so every ring produces a light-grey flash against
black. Those pixels are currently unreachable by any stylesheet — the overlay's
contents are inline `style="..."` attributes in the markup — which is why the
flash was never fixable from either side.

## Goals

- The embedding page controls colours, fonts, **and layout** of the doorbell page.
- Live theme changes apply without a reload.
- With zero messages received, the page renders exactly as it does today
  (the Chromecast/DashCast path receives nothing and must not regress).
- The contract survives quick replies being added, removed, and reordered
  through the web config.
- Nothing weakens the page's secure context — WebRTC and mic capture depend on it.

## Non-goals

- `?chrome=bare` or any parameter that hides the quick-reply panel. Replies stay;
  the embedder repositions them instead.
- A forced page state for screenshot testing. The dashboard's `cameras` screen
  reaches the live state without a doorbell press, and an unreachable
  `doorbell.camera_url` reaches the error state, so a test-only code path would
  add drift for no coverage.
- Theming the Home Assistant embed. See *Home Assistant* below — it is in scope
  as a consumer of the DOM contract, not as a themer, in this pass.

## Approach

### Why not a fixed variable contract

The obvious design is a fixed set of `--doorbell-*` values the parent supplies.
It was rejected because it cannot express layout or fonts, and because each of
the dashboard's three embed sites wants a *different* arrangement of the same
page: a full-bleed 1920×1080 cameras board, a 75vh ring modal, and a
`broadsheet` screen with an 8px border. One set of values cannot serve three
layouts, and every new thing the embedder wanted to restyle would mean another
variable and another round of coordinated changes across two repos.

### Chosen: one channel carrying raw CSS

The parent posts a stylesheet; the page injects it into a single
`<style id="doorbell-override">` appended last in `<head>`, replacing its
contents on each message. Live switching and the dashboard's drag-to-preview fall
out of this for free.

The variable layer lives *inside* this channel rather than beside it. The page
declares `--doorbell-*` internally with today's colours as defaults, so a simple
theme is a two-line payload and an ambitious one is more CSS in the same message.
One mechanism, two levels of effort.

**Variables are primary; layout CSS is the escape hatch.** Colours must never
depend on the page's DOM structure, so a future restructure can cost the embedder
positioning but never legibility.

```
--doorbell-bg           #d0d0d0          page background, loading overlay
--doorbell-surface      #c0c0c0          sidebar
--doorbell-text         #444             headings
--doorbell-text-muted   #555             overlay text, mute button
--doorbell-accent       #3a7bd5          quick-reply buttons, spinner
--doorbell-accent-text  #fff             text on accent
--doorbell-danger       #c0392b          talk idle
--doorbell-success      #27ae60          talk active, reply playing
--doorbell-border       rgba(0,0,0,0.1)  button borders
```

Hover and active shades derive from these via `color-mix()` rather than adding
variables.

## Protocol

Page → parent:

```js
{ type: 'doorbell:ready', contract: 1 }        // page can accept styling
{ type: 'doorbell:video-playing' }             // first frame rendered
```

Parent → page, any number of times:

```js
{ type: 'doorbell:style', css: '...' }
```

The `ready` handshake exists because the ring modal mounts a fresh iframe on every
press — a fire-and-retry parent would leave an unstyled window on every doorbell
press, the one moment it is guaranteed to be seen.

**The contract version travels in the message, not the DOM.** The embed is
cross-origin, so a `data-doorbell-contract` attribute is unreadable by the parent
and could not drive its fallback. It is mirrored onto the root element for local
debugging only; the message field is load-bearing. On an unknown version the
dashboard sends variables only and skips its layout CSS — degraded but legible,
which is what the variables-primary rule buys.

`doorbell-video-playing` (today a bare string, `webrtc-doorbell.html:217`) becomes
the object form with no transition period. Verified: no `addEventListener('message')`
exists in the dashboard frontend, and HA's only embed is `custom:doorbell-card`,
whose source (`doorbell-card.js`) has no message listener. HA's doorbell
automations are server-side YAML and cannot receive a browser `postMessage`.

### Origin allowlist

`server2.py` injects the allowlist into the served page from an environment
variable, defaulting to `https://dashboard.baldino.me`. Messages from other
origins are ignored, but the rejection is `console.warn`ed — otherwise a
misconfigured allowlist and a broken handshake are indistinguishable from inside
an iframe the embedder cannot inspect.

**Entries must be https.** `server2.py` warns at startup for any configured origin
that is neither https nor `http://localhost` (see below for why this is not
merely a policy preference).

## Secure context

WebRTC and `getUserMedia` require a secure context, and a document is only a
secure context if its own origin is trustworthy **and every ancestor is**. An
https iframe inside an `http://192.168.1.220:3042` parent is therefore *not* a
secure context: `RTCPeerConnection` and `getUserMedia` are both unavailable, so
the stream does not connect and the talk button cannot work. It also nullifies
the dashboard's approach of requesting mic permission on the parent page, since
`allow="microphone"` delegates a permission an insecure parent cannot hold.

Consequences:

1. The allowlist is https-only. `http://localhost:5173` is acceptable (localhost
   is treated as potentially trustworthy); `http://192.168.1.220:5173` is not.
2. **Diagnostic, outside this change:** if the kitchen tablet loads the dashboard
   over plain http, the doorbell embed is already broken there today. This should
   be checked on the device; neither agent can determine it from either repo.
3. Injected CSS referencing `http://` subresources is blocked as mixed content.
   This degrades the font silently but does not affect the page's secure context.
   The dashboard will serve fonts from `https://dashboard.baldino.me` for this
   reason.

Nothing else in this design introduces a new origin, scheme, or transport — it
adds a message listener and a `<style>` element.

## DOM contract, version 1

Hooks are `data-*` attributes rather than classes. They read unambiguously as API
rather than as the page's own styling, and an attribute selector has specificity
(0,1,0) — identical to the page's single-class base rules. Since the override
sheet is injected last, it wins ties by document order, with no specificity arms
race and no `!important` on either side.

| Attribute | Element |
|---|---|
| `data-doorbell="root"` | page wrapper; background, font, scoping ancestor |
| `data-doorbell="stage"` | wrapper around the video (**new** — none exists today) |
| `data-doorbell="video"` | the `<video>` element |
| `data-doorbell="sidebar"` | the panel |
| `data-doorbell="replies-heading"` | "Quick Replies" heading |
| `data-doorbell="replies"` | quick-reply container |
| `data-doorbell="reply"` | each reply button |
| `data-doorbell="controls"` | talk/mute container |
| `data-doorbell="talk"` / `"mute"` | individual buttons |
| `data-doorbell="overlay"` | loading overlay |
| `data-doorbell="debug"` | the stats overlay |

`replies` and `controls` are deliberately separate containers so quick replies and
talk/mute can be positioned independently — the ring modal wants the talk button
prominent and the replies incidental.

State hooks, since connect/disconnect transitions are where foreign styling shows
through:

- `data-doorbell-state="loading|connecting|live|error"` on root
- `data-doorbell-talking` on the talk button while transmitting
- `data-doorbell-reply-count="N"` on the replies container

### Genericity rules

Quick replies are user-configured through `messages.html`. `get_slots`
(`server2.py:295`) returns an unbounded, user-ordered list filtered for file
existence, with arbitrary ids and arbitrary text. It is fetched *after* first
paint (`webrtc-doorbell.html:203`), so the set is empty at load and populates
later. The contract must therefore hold at N=0 as well as N=8:

- **Layout attaches to containers, never to children.** No CSS on either side may
  assume a count: no fixed heights, no structural `nth-child` layout, no sidebar
  width derived from the number of buttons.
- **The count is exposed, not inferred.** `data-doorbell-reply-count` is `0` at
  load and updated when the fetch resolves, so an embedder can vary layout by
  count without inspecting DOM it cannot read.
- **N=0 is a real state.** "Quick Replies" is currently unconditional markup and
  would float over an empty container. Base CSS handles the empty case.
- **Buttons wrap; they do not size the panel.** Reply text is user-authored and
  may be long.
- **`data-doorbell-reply="<slug>"` is cosmetic, not load-bearing.** Slugs are
  user-editable and a reply can be deleted at any time, so layout built on a
  specific slug silently stops applying. Documented as such.
- **Reply order belongs to the user.** `set_slots` records a deliberate ordering;
  an embedder using CSS `order:` overrides a user configuration choice.

## Inline styles must go

JS-set inline styles beat any injected stylesheet regardless of specificity, and
the failure is silent. Current uses:

| Location | Use | Disposition |
|---|---|---|
| `:75` | `talk.style.display='none'` for `?talk=0` | attribute toggle |
| `:149` | mic-error background | `data-doorbell-state="error"` + CSS |
| `:214` | overlay `opacity=0` fade | **kept**, documented as the page's |
| `:47-51` | overlay spinner + text, inline attrs in markup | moved to CSS |

The `:47-51` block is the important one: it is the light-grey flash on the ring
modal's black scrim, and it is unreachable by any stylesheet today.

Base CSS drops to single-class/attribute specificity with no `!important`, or
overrides do not reliably win. This refactor is the bulk of the work; the message
plumbing is small.

## Home Assistant

HA is a second framer of the same page:

```
lovelace.lovelace.20260307_235948:285-286
  "type": "custom:doorbell-card",
  "url": "https://cast.baldino.me/webrtc-doorbell.html?v=2"
```

For this pass HA is **not** themed: its origin is not in the default allowlist, so
its embed keeps today's appearance. This is a recorded decision rather than an
oversight — `doorbell-card.js` is the natural place to grow the same theming
later, since HA exposes real theme variables the card could forward through this
identical channel.

The DOM contract consequently has two consumers, not one.

*Caveat:* `ha-config/` is not mounted in this checkout, so the HA findings above
come from `ha-config-backup/` and from `doorbell-card.js` in this repo, not from
the copy deployed in HA's `www/`.

## Accepted costs

- **The DOM becomes published API.** Once layout CSS is written against
  `data-doorbell="stage"`, that markup cannot be restructured without silently
  breaking the embedder's theme, and neither repo's tests can catch it — the
  coupling is invisible to both. Contract versioning bounds the blast radius; it
  does not remove it. This is a standing tax on future changes to this page.
- **Arbitrary CSS from the framer** can hide the talk button or pull remote
  images. The framer already embeds the page, so this formalises an existing
  capability rather than granting a new one; the https-only allowlist bounds who
  holds it.
- **Fonts depend on work in the dashboard repo** — stable paths under
  `frontend/public/fonts/` and a CORS layer on its Rust backend. Until both land,
  `@font-face` payloads silently fall back. Not verifiable from this side.

## Testing

- Page renders identically to today with no messages received (Chromecast path).
- A variables-only payload recolours without touching layout.
- A layout payload repositions `replies` independently of `controls`.
- A second payload replaces the first (live switching, no reload).
- Messages from a non-allowlisted origin are ignored and warned.
- Rendering is correct at 0, 1, and many replies, and when the count changes
  after load.
- No inline style beyond the overlay fade survives, verified by inspection.
- `server2.py` warns on a non-https allowlist entry.

## Open items

- The kitchen tablet's actual origin, and whether it is https (blocks nothing
  here; determines whether the embed works at all).
- Dashboard to confirm the DOM contract list, per its offer to review it as a
  list rather than discover gaps mid-build.
