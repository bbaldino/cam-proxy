# Doorbell Theming Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a cross-origin embedder control the doorbell page's colours, fonts, and layout by posting it a stylesheet, without changing how the page looks when nobody does.

**Architecture:** `webrtc-doorbell.html` publishes a `data-doorbell` DOM tree and a `--doorbell-*` variable layer, then accepts `{type:'doorbell:style', css}` from an allowlisted origin and injects it into a single `<style>` appended last in `<head>`. `server2.py` injects the origin allowlist into the served page from an environment variable. All existing colours become variable defaults, so a page that receives no messages renders exactly as it does today.

**Tech Stack:** Plain HTML/CSS/JS (no build step), Python 3.12 + aiohttp, pytest for the server-side pure functions.

**Spec:** `docs/superpowers/specs/2026-08-01-doorbell-theming-contract-design.md`

## Global Constraints

- **Zero visual change with no messages received.** Every current colour becomes a variable *default*. The Chromecast/DashCast path receives nothing and must look identical after this change.
- **No `!important` anywhere in the base stylesheet.** The override sheet wins by document order, not specificity.
- **No id selectors for anything themeable.** `#sidebar` is (1,0,0) and would beat an embedder's `[data-doorbell="sidebar"]` at (0,1,0). Ids stay in the markup for `getElementById` only. Base rules select on `data-doorbell` attributes so both sheets sit at (0,1,0).
- **No inline styles.** No `style="..."` attributes in markup and no `element.style.x =` in JS. Both silently beat any stylesheet. Use attribute toggles.
- **`font-family` is set in exactly three rules** — root, headings, stats overlay — and nowhere else.
- **No `:has()`.** Older Chromecast browsers may not support it; `data-doorbell-reply-count` is mirrored onto root so descendant selectors suffice.
- **Every element with a labelled descendant is itself labelled.** An unlabelled wrapper is a dead spot an embedder cannot reach around.
- **Allowlisted origins must be https** (or `http://localhost` / `http://127.0.0.1`). An https iframe inside an http parent is not a secure context, so WebRTC and `getUserMedia` are unavailable entirely.
- Contract version is **1**, and it travels in the `doorbell:ready` *message*. The DOM mirror is debug-only.

---

### Task 1: Origin allowlist module

A standalone module so the parsing, validation, and injection logic is testable without importing `server2.py` (which pulls in aiohttp, pychromecast, and gtts).

**Files:**
- Create: `theme_origins.py`
- Create: `tests/test_theme_origins.py`
- Create: `pytest.ini`
- Create: `requirements-dev.txt`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `DEFAULT_THEME_ORIGINS: str`
  - `parse_theme_origins(raw: str) -> list[str]`
  - `is_secure_origin(origin: str) -> bool`
  - `insecure_origins(origins: list[str]) -> list[str]`
  - `inject_theme_origins(html: str, origins: list[str]) -> str`

- [ ] **Step 1: Add dev tooling and ignore rules**

Create `requirements-dev.txt`:

```
pytest
```

Create `pytest.ini`:

```ini
[pytest]
pythonpath = .
testpaths = tests
```

Append to `.gitignore`:

```
__pycache__/
venv/
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_theme_origins.py`:

```python
import pytest

from theme_origins import (
    DEFAULT_THEME_ORIGINS,
    inject_theme_origins,
    insecure_origins,
    is_secure_origin,
    parse_theme_origins,
)


def test_default_is_the_dashboard_https_origin():
    assert parse_theme_origins(DEFAULT_THEME_ORIGINS) == [
        "https://dashboard.baldino.me"
    ]


def test_parses_comma_separated_and_trims_whitespace():
    raw = " https://a.example.com , https://b.example.com "
    assert parse_theme_origins(raw) == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_strips_trailing_slash_so_it_matches_event_origin():
    # window.postMessage event.origin never has a trailing slash.
    assert parse_theme_origins("https://a.example.com/") == ["https://a.example.com"]


def test_empty_string_yields_no_origins():
    assert parse_theme_origins("") == []
    assert parse_theme_origins("  ,  ") == []


@pytest.mark.parametrize(
    "raw",
    [
        'https://a.example.com" onload="alert(1)',
        "javascript:alert(1)",
        "not-a-url",
        "https://a.example.com/path",
        "https://a.example.com two",
    ],
)
def test_rejects_malformed_or_unsafe_entries(raw):
    # Origins land in an HTML attribute, so anything that is not a bare
    # scheme://host[:port] is dropped rather than escaped.
    assert parse_theme_origins(raw) == []


def test_keeps_valid_entries_alongside_rejected_ones():
    raw = "https://good.example.com,not-a-url,https://also-good.example.com:8443"
    assert parse_theme_origins(raw) == [
        "https://good.example.com",
        "https://also-good.example.com:8443",
    ]


@pytest.mark.parametrize(
    "origin",
    [
        "https://dashboard.baldino.me",
        "https://dashboard.baldino.me:8443",
        "http://localhost",
        "http://localhost:5173",
        "http://127.0.0.1:3042",
    ],
)
def test_secure_origins(origin):
    assert is_secure_origin(origin) is True


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.1.220:3042",
        "http://192.168.1.220:5173",
        "http://dashboard.baldino.me",
    ],
)
def test_insecure_origins_rejected(origin):
    # An https iframe inside an http parent is not a secure context at all:
    # RTCPeerConnection and getUserMedia both become unavailable.
    assert is_secure_origin(origin) is False


def test_insecure_origins_lists_only_the_bad_ones():
    origins = ["https://good.example.com", "http://192.168.1.220:3042"]
    assert insecure_origins(origins) == ["http://192.168.1.220:3042"]


def test_injects_origins_into_the_meta_tag():
    html = '<head><meta name="doorbell-theme-origins" content=""></head>'
    out = inject_theme_origins(html, ["https://a.example.com", "https://b.example.com"])
    assert (
        '<meta name="doorbell-theme-origins" '
        'content="https://a.example.com,https://b.example.com">' in out
    )


def test_injection_replaces_rather_than_appends():
    html = '<meta name="doorbell-theme-origins" content="https://stale.example.com">'
    out = inject_theme_origins(html, ["https://fresh.example.com"])
    assert "stale.example.com" not in out
    assert "fresh.example.com" in out


def test_injection_with_no_origins_leaves_an_empty_attribute():
    html = '<meta name="doorbell-theme-origins" content="https://a.example.com">'
    assert 'content=""' in inject_theme_origins(html, [])


def test_injection_is_a_noop_when_the_meta_tag_is_absent():
    html = "<head><title>x</title></head>"
    assert inject_theme_origins(html, ["https://a.example.com"]) == html
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_theme_origins.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'theme_origins'`

- [ ] **Step 4: Write the implementation**

Create `theme_origins.py`:

```python
"""Origin allowlist for the doorbell page's theming channel.

Kept separate from server2.py so it can be tested without importing aiohttp,
pychromecast, and gtts.
"""
import re

DEFAULT_THEME_ORIGINS = "https://dashboard.baldino.me"

# A bare origin: scheme://host[:port]. No path, no query, no whitespace, no
# quotes. Origins are interpolated into an HTML attribute, so anything that is
# not this shape is dropped rather than escaped.
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$")

_META_RE = re.compile(r'(<meta name="doorbell-theme-origins" content=")[^"]*(">)')

# Hosts a browser treats as potentially trustworthy over plain http.
_LOCAL_HOSTS = ("localhost", "127.0.0.1")


def parse_theme_origins(raw):
    """Parse a comma-separated allowlist, dropping anything malformed."""
    origins = []
    for entry in raw.split(","):
        candidate = entry.strip().rstrip("/")
        if candidate and _ORIGIN_RE.match(candidate):
            origins.append(candidate)
    return origins


def is_secure_origin(origin):
    """True if a parent frame on this origin can host a secure-context iframe.

    A document is only a secure context if its own origin is trustworthy *and*
    every ancestor is. An https doorbell page inside an http parent therefore
    loses RTCPeerConnection and getUserMedia entirely.
    """
    if origin.startswith("https://"):
        return True
    if origin.startswith("http://"):
        host = origin[len("http://") :].split(":")[0]
        return host in _LOCAL_HOSTS
    return False


def insecure_origins(origins):
    """Return the subset of origins that cannot host a secure-context iframe."""
    return [o for o in origins if not is_secure_origin(o)]


def inject_theme_origins(html, origins):
    """Replace the doorbell-theme-origins meta tag's content attribute."""
    joined = ",".join(origins)
    return _META_RE.sub(lambda m: m.group(1) + joined + m.group(2), html)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_theme_origins.py -v`
Expected: PASS, 23 tests (13 plain plus 10 from the three parametrised cases)

- [ ] **Step 6: Commit**

```bash
git add theme_origins.py tests/test_theme_origins.py pytest.ini requirements-dev.txt .gitignore
git commit -m "feat: origin allowlist module for doorbell theming channel"
```

---

### Task 2: Markup and stylesheet refactor

The bulk of the work, and entirely presentational: after this task the page must look *identical* while every colour has become a variable and every hook exists.

**Files:**
- Modify: `webrtc-doorbell.html:1-68` (head, style block, and body markup)

**Interfaces:**
- Consumes: nothing
- Produces: the `data-doorbell` DOM tree and `--doorbell-*` variables that Tasks 3, 4, and 6 rely on. The ids (`#video`, `#dbg`, `#dbg-toggle`, `#mute`, `#talk`, `#messages`, `#loading-overlay`) are preserved because the existing JS looks them up by id.

- [ ] **Step 1: Replace everything from `<head>` through the opening of `<script>`**

Replace `webrtc-doorbell.html` lines 1–68 with:

```html
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="doorbell-theme-origins" content="">
  <link rel="icon" href="data:,">
  <title>WebRTC Doorbell</title>
  <style>
    /* Theming contract v1. Every value below is a default: an embedder may
       override any of them by posting CSS. See docs/theming.md.
       Rules select on [data-doorbell] attributes, not ids, so the injected
       sheet sits at the same specificity and wins by document order. */
    :root {
      --doorbell-bg: #d0d0d0;
      --doorbell-surface: #c0c0c0;
      --doorbell-text: #444;
      --doorbell-text-muted: #555;
      --doorbell-accent: #3a7bd5;
      --doorbell-accent-text: #fff;
      --doorbell-danger: #c0392b;
      --doorbell-success: #27ae60;
      --doorbell-border: rgba(0, 0, 0, 0.1);
      --doorbell-font-body: sans-serif;
      --doorbell-font-display: sans-serif;
      --doorbell-font-mono: monospace;
    }

    * { margin: 0; padding: 0; box-sizing: border-box; }
    html { width: 100%; height: 100%; }

    [data-doorbell="root"] {
      width: 100%; height: 100%; overflow: hidden;
      background: var(--doorbell-bg);
      font-family: var(--doorbell-font-body);
    }

    [data-doorbell="layout"] {
      display: flex; width: 100%; height: 100%;
      justify-content: center; position: relative;
    }

    [data-doorbell="stage"] { position: relative; height: 100%; }
    [data-doorbell="video"] { height: 100%; object-fit: contain; display: block; }

    [data-doorbell="debug"] {
      position: absolute; top: 4px; left: 4px;
      color: lime; font-family: var(--doorbell-font-mono); font-size: 11px;
      background: rgba(0, 0, 0, 0.6); padding: 4px 6px; z-index: 40;
      white-space: pre; display: none;
    }
    /* (0,2,0) — deliberately outranks an embedder hiding [data-doorbell="debug"].
       To suppress diagnostics entirely, hide [data-doorbell="debug-toggle"] so
       the overlay can never be switched on. */
    [data-doorbell="debug"][data-visible] { display: block; }

    [data-doorbell="debug-toggle"] {
      position: absolute; bottom: 6px; right: 6px; width: 28px; height: 28px;
      background: rgba(0, 0, 0, 0.15); border: none; border-radius: 6px;
      color: rgba(0, 0, 0, 0.4); font-size: 13px; cursor: pointer;
      z-index: 20; line-height: 28px; text-align: center;
    }
    [data-doorbell="debug-toggle"]:hover {
      background: rgba(0, 0, 0, 0.3); color: var(--doorbell-text);
    }

    [data-doorbell="sidebar"] {
      width: 200px; flex-shrink: 0; background: var(--doorbell-surface);
      display: flex; flex-direction: column; padding: 12px; gap: 10px;
      overflow-y: auto; justify-content: center; height: 100%;
    }

    [data-doorbell="replies-heading"] {
      color: var(--doorbell-text); font-family: var(--doorbell-font-display);
      font-size: 13px; margin: 0; text-align: center;
    }

    [data-doorbell="replies"] { display: flex; flex-direction: column; gap: 10px; }

    [data-doorbell="reply"] {
      color: var(--doorbell-accent-text); font-size: 14px;
      background: var(--doorbell-accent);
      border: 1px solid var(--doorbell-border); border-radius: 8px;
      padding: 14px 10px; cursor: pointer; user-select: none;
      text-align: center; line-height: 1.3;
      overflow-wrap: anywhere;
    }
    [data-doorbell="reply"]:hover {
      background: color-mix(in srgb, var(--doorbell-accent) 85%, white);
    }
    [data-doorbell="reply"][data-doorbell-playing] {
      background: var(--doorbell-success);
    }

    /* Replies are user-configured and may be empty; hide the heading rather
       than let it float over nothing. Hidden by rule, not removed from the DOM. */
    [data-doorbell-reply-count="0"] [data-doorbell="replies-heading"],
    [data-doorbell-reply-count="0"] [data-doorbell="replies"] { display: none; }

    [data-doorbell="controls"] {
      display: flex; gap: 10px; justify-content: center; margin-top: 10px;
    }

    [data-doorbell="talk"], [data-doorbell="mute"] {
      color: var(--doorbell-accent-text); font-size: 14px;
      border: none; border-radius: 8px; padding: 8px 0; width: 80px;
      text-align: center; cursor: pointer; user-select: none;
    }
    [data-doorbell="talk"] { background: var(--doorbell-danger); }
    [data-doorbell="talk"][data-doorbell-talking] { background: var(--doorbell-success); }
    [data-doorbell="talk"][data-doorbell-mic="denied"] {
      background: color-mix(in srgb, var(--doorbell-danger) 50%, var(--doorbell-surface));
    }
    [data-doorbell="mute"] { background: var(--doorbell-text-muted); }
    [data-doorbell="mute"][data-doorbell-muted] {
      background: color-mix(in srgb, var(--doorbell-text-muted) 70%, black);
    }
    [data-doorbell-talk="off"] [data-doorbell="talk"] { display: none; }

    [data-doorbell="overlay"] {
      position: absolute; top: 0; left: 0; width: 100%; height: 100%;
      background: var(--doorbell-bg); display: flex;
      align-items: center; justify-content: center; z-index: 30;
      transition: opacity 0.3s ease;
    }
    [data-doorbell-state="live"] [data-doorbell="overlay"] { opacity: 0; }
    [data-doorbell="overlay-inner"] { text-align: center; color: var(--doorbell-text-muted); }
    [data-doorbell="spinner"] {
      width: 48px; height: 48px; margin: 0 auto 16px;
      border: 4px solid var(--doorbell-border);
      border-top-color: var(--doorbell-accent);
      border-radius: 50%; animation: doorbell-spin 0.8s linear infinite;
    }
    [data-doorbell="overlay-text"] { font-size: 14px; }

    @keyframes doorbell-spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body data-doorbell="root" data-doorbell-contract="1" data-doorbell-state="loading"
      data-doorbell-talk="on" data-doorbell-reply-count="0">
  <div id="loading-overlay" data-doorbell="overlay">
    <div data-doorbell="overlay-inner">
      <div data-doorbell="spinner"></div>
      <div data-doorbell="overlay-text">Connecting to doorbell...</div>
    </div>
  </div>
  <div id="layout" data-doorbell="layout">
    <div id="video-container" data-doorbell="stage">
      <div id="dbg" data-doorbell="debug">Starting...</div>
      <video id="video" data-doorbell="video" autoplay playsinline muted></video>
    </div>
    <div id="sidebar" data-doorbell="sidebar">
      <h3 data-doorbell="replies-heading">Quick Replies</h3>
      <div id="messages" data-doorbell="replies" data-doorbell-reply-count="0"></div>
      <div id="controls" data-doorbell="controls">
        <div id="mute" data-doorbell="mute" data-doorbell-muted>UNMUTE</div>
        <div id="talk" data-doorbell="talk">TALK</div>
      </div>
    </div>
    <button id="dbg-toggle" data-doorbell="debug-toggle" title="Toggle debug overlay">i</button>
  </div>
```

Note: `class="btn"` is gone from the mute and talk elements — they are styled by attribute now. `data-doorbell-muted` is present initially because the `<video>` starts muted.

- [ ] **Step 2: Verify no forbidden constructs remain in the style block or markup**

Run:

```bash
grep -n 'style="' webrtc-doorbell.html
grep -n '!important' webrtc-doorbell.html
grep -nE '^\s*#[a-z-]+ *[,{]' webrtc-doorbell.html
grep -c 'font-family' webrtc-doorbell.html
```

Expected: the first three produce **no output**; `font-family` count is exactly **3** — root, `replies-heading`, and `debug`. (The `--doorbell-font-*` declarations do not contain the string.) A fourth match means a stray rule that would silently beat an embedder's font override.

- [ ] **Step 3: Verify visual parity**

The JS still references `.classList` and `.style` at this point, so the page is mid-refactor — but the *initial* render must be pixel-identical.

Run: `python server2.py` and open `http://localhost:8899/webrtc-doorbell.html`

Expected: identical to before — light-grey page, grey sidebar, blue reply buttons, red TALK, grey UNMUTE, spinner over the grey overlay. Compare against `git stash` of this change if unsure.

- [ ] **Step 4: Commit**

```bash
git add webrtc-doorbell.html
git commit -m "refactor: doorbell page markup and CSS onto the data-doorbell contract"
```

---

### Task 3: State hooks and reply count in JS

Removes every remaining inline-style write and publishes the state attributes.

**Files:**
- Modify: `webrtc-doorbell.html` (script block)

**Interfaces:**
- Consumes: the DOM tree from Task 2
- Produces: `setState(name)` helper; the `data-doorbell-state`, `data-doorbell-talking`, `data-doorbell-muted`, `data-doorbell-mic`, `data-doorbell-playing`, `data-doorbell-talk`, and `data-doorbell-reply-count` attributes that Task 6's harness asserts against.

- [ ] **Step 1: Add the root handle and state helper**

Immediately after `const showTalk = params.get('talk') !== '0';`, add:

```js
const root = document.body;
const setState = (s) => root.setAttribute('data-doorbell-state', s);
```

- [ ] **Step 2: Replace the `?talk=0` inline style**

Replace:

```js
if (!showTalk) document.getElementById('talk').style.display = 'none';
```

with:

```js
root.setAttribute('data-doorbell-talk', showTalk ? 'on' : 'off');
```

- [ ] **Step 3: Convert the debug overlay toggles from classes to attributes**

Replace `if (debugAlways) dbg.classList.add('always');` with:

```js
if (debugAlways) dbg.setAttribute('data-visible', '');
```

Replace the toggle handler body `dbg.classList.toggle('visible');` with:

```js
dbg.toggleAttribute('data-visible');
```

- [ ] **Step 4: Publish the mute state**

Replace the `volumechange` handler with:

```js
video.addEventListener('volumechange', () => {
  muteBtn.textContent = video.muted ? 'UNMUTE' : 'MUTE';
  muteBtn.toggleAttribute('data-doorbell-muted', video.muted);
});
```

- [ ] **Step 5: Publish the talking and mic-denied states**

In `startTalking`, replace the error branch's inline style:

```js
      } catch (e) {
        console.warn('No mic access:', e);
        talkBtn.textContent = e.message || e.name;
        talkBtn.setAttribute('data-doorbell-mic', 'denied');
        return;
      }
```

Replace `talkBtn.classList.add('active');` with `talkBtn.toggleAttribute('data-doorbell-talking', true);`
Replace `talkBtn.classList.remove('active');` with `talkBtn.toggleAttribute('data-doorbell-talking', false);`

- [ ] **Step 6: Publish the reply playing state and the reply hooks**

In `playMessage`, replace `btn.classList.add('playing');` with `btn.toggleAttribute('data-doorbell-playing', true);` and both `btn.classList.remove('playing');` calls with `btn.toggleAttribute('data-doorbell-playing', false);`

Replace `addMessageBtn` and the fetch that drives it with:

```js
  function addMessageBtn(msg) {
    const container = document.getElementById('messages');
    const div = document.createElement('div');
    div.dataset.doorbell = 'reply';
    // Cosmetic hook only: slugs are user-editable and a reply can be deleted
    // at any time, so layout must never depend on a specific one.
    div.dataset.doorbellReply = msg.id;
    div.textContent = msg.text;
    div.addEventListener('click', () => playMessage(div));
    container.appendChild(div);
  }

  fetch(`${baseUrl}/api/slots`).then(r => r.json()).then(msgs => {
    msgs.forEach(addMessageBtn);
    // Mirrored onto root so descendant selectors work without :has(),
    // which older Chromecast browsers may not support.
    const count = String(msgs.length);
    document.getElementById('messages').setAttribute('data-doorbell-reply-count', count);
    root.setAttribute('data-doorbell-reply-count', count);
  });
```

- [ ] **Step 7: Drive the overlay from state instead of an inline opacity write**

Replace the `playing` listener body's overlay handling:

```js
  video.addEventListener('playing', () => {
    if (!firstFrameLogged) {
      firstFrameLogged = true;
      log(`[${elapsed()}s] First frame playing!`);
      setState('live');
      const overlay = document.getElementById('loading-overlay');
      if (overlay) setTimeout(() => overlay.remove(), 300);
      window.parent.postMessage('doorbell-video-playing', '*');
    }
  });
```

The CSS rule `[data-doorbell-state="live"] [data-doorbell="overlay"] { opacity: 0 }` now drives the fade; the timeout only removes the element afterwards.

- [ ] **Step 8: Publish the remaining lifecycle transitions**

At the top of `connect()`, after `log(...)`, add:

```js
  setState('connecting');
```

In `oniceconnectionstatechange`, in the `failed` branch, add `setState('error');` alongside the existing log.

In the early-return for a non-ok go2rtc response, add `setState('error');` before `return;`.

Change the bottom-of-file call to:

```js
connect().catch(e => { setState('error'); log(`Error: ${e.message}`); });
```

- [ ] **Step 9: Verify no inline styles or stale classes remain**

Run:

```bash
grep -n '\.style\.' webrtc-doorbell.html
grep -n 'classList' webrtc-doorbell.html
grep -n 'style="' webrtc-doorbell.html
```

Expected: **no output from any of them.**

- [ ] **Step 10: Verify behaviour in the browser**

Run: `python server2.py`, open `http://localhost:8899/webrtc-doorbell.html`, open devtools.

Expected, checked on `<body>` in the element inspector:
- `data-doorbell-state` moves `loading` → `connecting` → `live`
- overlay fades and is removed on first frame
- clicking UNMUTE toggles `data-doorbell-muted` on `#mute` and the label
- holding TALK toggles `data-doorbell-talking` on `#talk`
- `data-doorbell-reply-count` matches the number of reply buttons, on both `<body>` and `#messages`
- opening with `?talk=0` sets `data-doorbell-talk="off"` and hides the talk button

- [ ] **Step 11: Commit**

```bash
git add webrtc-doorbell.html
git commit -m "refactor: publish doorbell state as attributes, drop all inline styles"
```

---

### Task 4: The theming channel

**Files:**
- Modify: `webrtc-doorbell.html` (script block)

**Interfaces:**
- Consumes: the `<meta name="doorbell-theme-origins">` tag from Task 2
- Produces: the `doorbell:ready` / `doorbell:style` / `doorbell:video-playing` protocol that Task 5 serves origins for and Task 6 exercises

- [ ] **Step 1: Add the channel at the top of the script block**

Immediately after the `const root = ...` / `setState` lines from Task 3, add:

```js
// --- Theming channel (contract v1) ---
// An embedder posts {type:'doorbell:style', css} and we inject it into a single
// <style> appended last in <head>, so it wins ties by document order. With no
// messages received the page keeps its defaults, which is the Chromecast path.
const THEME_CONTRACT = 1;
const THEME_ORIGINS = (
  document.querySelector('meta[name="doorbell-theme-origins"]')?.content || ''
).split(',').map(s => s.trim()).filter(Boolean);

const overrideStyle = document.createElement('style');
overrideStyle.id = 'doorbell-override';
document.head.appendChild(overrideStyle);

window.addEventListener('message', (ev) => {
  const msg = ev.data;
  // Check the type before the origin so unrelated cross-frame chatter does not
  // fill the console with allowlist warnings.
  if (!msg || typeof msg !== 'object' || msg.type !== 'doorbell:style') return;
  if (!THEME_ORIGINS.includes(ev.origin)) {
    console.warn(`[doorbell] ignoring doorbell:style from non-allowlisted origin: ${ev.origin}`);
    return;
  }
  if (typeof msg.css !== 'string') return;
  overrideStyle.textContent = msg.css;
});

window.parent.postMessage({ type: 'doorbell:ready', contract: THEME_CONTRACT }, '*');
```

- [ ] **Step 2: Migrate the video-playing message to the object form**

Replace:

```js
      window.parent.postMessage('doorbell-video-playing', '*');
```

with:

```js
      window.parent.postMessage({ type: 'doorbell:video-playing' }, '*');
```

- [ ] **Step 3: Verify the bare string is gone**

Run: `grep -n "doorbell-video-playing" webrtc-doorbell.html`
Expected: no output. (Nothing listens for it — verified across cam-proxy, the dashboard frontend, and HA's `doorbell-card.js`.)

- [ ] **Step 4: Verify the channel rejects an unconfigured origin**

The allowlist is empty until Task 5 wires the server, so every message must be rejected right now — which is exactly the negative case.

Run: `python server2.py`, open `http://localhost:8899/webrtc-doorbell.html`, and in the console:

```js
window.postMessage({type: 'doorbell:style', css: ':root{--doorbell-bg:red}'}, '*')
```

Expected: a `[doorbell] ignoring doorbell:style from non-allowlisted origin: http://localhost:8899` warning, and the background does **not** change.

Then run: `window.postMessage({type: 'something-else'}, '*')`
Expected: **no** warning (type is checked first).

- [ ] **Step 5: Commit**

```bash
git add webrtc-doorbell.html
git commit -m "feat: doorbell theming channel with origin allowlist"
```

---

### Task 5: Serve the allowlist

**Files:**
- Modify: `server2.py:13-23` (config), `server2.py:487` (routes)
- Modify: `Dockerfile:6`

**Interfaces:**
- Consumes: `theme_origins` from Task 1, the meta tag from Task 2
- Produces: `/webrtc-doorbell.html` served with the allowlist injected

- [ ] **Step 1: Import and configure**

In the import block at the top of `server2.py`, after `from gtts import gTTS`, add:

```python
from theme_origins import (
    DEFAULT_THEME_ORIGINS,
    inject_theme_origins,
    insecure_origins,
    parse_theme_origins,
)
```

After the `RTSP_PROXY_PORT` line, add:

```python
# Origins permitted to post theming CSS into the doorbell page.
THEME_ORIGINS = parse_theme_origins(
    os.environ.get("DOORBELL_THEME_ORIGINS", DEFAULT_THEME_ORIGINS)
)
for _origin in insecure_origins(THEME_ORIGINS):
    print(
        f"WARNING: theme origin {_origin} is not a secure origin. A parent frame "
        f"there cannot host WebRTC or microphone capture in the doorbell iframe, "
        f"because a document nested in an insecure ancestor is not a secure context."
    )
```

- [ ] **Step 2: Add the handler**

Immediately before the `app = web.Application(...)` line, add:

```python
DOORBELL_PAGE = os.path.join(SERVE_DIR, "webrtc-doorbell.html")


async def serve_doorbell(request):
    """Serve the doorbell page with the theming origin allowlist injected."""
    with open(DOORBELL_PAGE, encoding="utf-8") as f:
        html = f.read()
    return web.Response(
        text=inject_theme_origins(html, THEME_ORIGINS), content_type="text/html"
    )
```

- [ ] **Step 3: Register the route before the static catch-all**

aiohttp resolves routes in registration order, so this must come *above* `add_static`. Replace the final route line with:

```python
app.router.add_get("/webrtc-doorbell.html", serve_doorbell)
app.router.add_static("/", SERVE_DIR, show_index=True)
```

- [ ] **Step 4: Ship the new module in the image**

`theme_origins.py` is imported at startup — without this the container fails to boot. In `Dockerfile`, replace the `COPY` line with:

```dockerfile
COPY server2.py rtsp_proxy.py theme_origins.py cast.html webrtc-doorbell.html messages.html doorbell-card.js test-embed.html ./
```

(`test-embed.html` is created in Task 6; create an empty placeholder now with `touch test-embed.html` so the build does not break between tasks.)

- [ ] **Step 5: Verify injection and the insecure-origin warning**

Run:

```bash
python server2.py &
curl -s http://localhost:8899/webrtc-doorbell.html | grep doorbell-theme-origins
```

Expected: `<meta name="doorbell-theme-origins" content="https://dashboard.baldino.me">`

Then:

```bash
kill %1
DOORBELL_THEME_ORIGINS="http://192.168.1.220:3042" python server2.py
```

Expected: a `WARNING: theme origin http://192.168.1.220:3042 is not a secure origin` line at startup.

- [ ] **Step 6: Commit**

```bash
git add server2.py Dockerfile test-embed.html
git commit -m "feat: serve doorbell page with theming origin allowlist injected"
```

---

### Task 6: Embedder harness and contract documentation

The harness doubles as the reference parent implementation the dashboard can crib from, and is the only way to eyeball the ring-modal-on-black-scrim case that motivated the overlay fix.

**Files:**
- Create: `test-embed.html` (replacing the Task 5 placeholder)
- Create: `docs/theming.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the full protocol from Tasks 1–5
- Produces: nothing consumed by later tasks

- [ ] **Step 1: Write the harness page**

Create `test-embed.html`:

```html
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Doorbell Theming Harness</title>
  <style>
    body { font: 14px sans-serif; margin: 0; display: flex; height: 100vh; }
    #panel { width: 280px; padding: 16px; background: #222; color: #eee; overflow-y: auto; }
    #panel button { display: block; width: 100%; margin-bottom: 8px; padding: 8px; cursor: pointer; }
    #frame-wrap { flex: 1; background: #111; display: flex; align-items: center; justify-content: center; }
    iframe { width: 100%; height: 100%; border: none; }
    #frame-wrap.scrim { background: rgba(0,0,0,0.9); padding: 40px; }
    #frame-wrap.scrim iframe { border-radius: 16px; overflow: hidden; max-width: 900px; height: 75vh; }
    #status { font: 12px monospace; white-space: pre-wrap; margin-top: 12px; color: #9f9; }
  </style>
</head>
<body>
  <div id="panel">
    <h3>Theming harness</h3>
    <button data-payload="vars">1. Colours only (dark)</button>
    <button data-payload="paper">2. Colours only (warm paper)</button>
    <button data-payload="layout">3. Layout: sidebar to bottom</button>
    <button data-payload="split">4. Layout: replies left, controls right</button>
    <button data-payload="font">5. Fonts (generic families)</button>
    <button data-payload="clear">6. Clear override</button>
    <button id="scrim">Toggle black scrim / ring-modal framing</button>
    <div id="status">waiting for doorbell:ready...</div>
  </div>
  <div id="frame-wrap">
    <iframe id="frame" src="/webrtc-doorbell.html" allow="autoplay; camera; microphone"></iframe>
  </div>
<script>
const frame = document.getElementById('frame');
const status = document.getElementById('status');

const PAYLOADS = {
  vars: `:root{--doorbell-bg:#1a1e2a;--doorbell-surface:#232838;--doorbell-text:#e6e8ef;
    --doorbell-text-muted:#98a0b3;--doorbell-accent:#5b8def;--doorbell-accent-text:#fff;
    --doorbell-danger:#e05252;--doorbell-success:#3ecf8e;--doorbell-border:rgba(255,255,255,0.12)}`,
  paper: `:root{--doorbell-bg:#f6f1e7;--doorbell-surface:#ece5d8;--doorbell-text:#191512;
    --doorbell-text-muted:#6b6259;--doorbell-accent:#b43a1a;--doorbell-accent-text:#f6f1e7;
    --doorbell-danger:#b43a1a;--doorbell-success:#3f5d44;--doorbell-border:#cec9c1}`,
  layout: `[data-doorbell="layout"]{flex-direction:column}
    [data-doorbell="sidebar"]{width:100%;height:auto;flex-direction:row;justify-content:space-between;align-items:center}
    [data-doorbell="replies"]{flex-direction:row;flex-wrap:wrap}
    [data-doorbell="stage"]{height:auto;flex:1;min-height:0}`,
  split: `[data-doorbell="sidebar"]{width:320px}
    [data-doorbell="replies"]{display:grid;grid-template-columns:1fr 1fr}
    [data-doorbell="controls"]{flex-direction:column}`,
  font: `:root{--doorbell-font-body:Georgia,serif;--doorbell-font-display:"Courier New",monospace;
    --doorbell-font-mono:"Courier New",monospace}`,
  clear: ``,
};

function send(css) {
  frame.contentWindow.postMessage({ type: 'doorbell:style', css }, '*');
}

document.querySelectorAll('#panel button[data-payload]').forEach(btn => {
  btn.addEventListener('click', () => send(PAYLOADS[btn.dataset.payload]));
});

document.getElementById('scrim').addEventListener('click', () => {
  document.getElementById('frame-wrap').classList.toggle('scrim');
});

window.addEventListener('message', (ev) => {
  const msg = ev.data;
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'doorbell:ready') {
    status.textContent = `doorbell:ready (contract ${msg.contract})\norigin: ${ev.origin}`;
  } else if (msg.type === 'doorbell:video-playing') {
    status.textContent += '\ndoorbell:video-playing';
  }
});
</script>
</body>
</html>
```

- [ ] **Step 2: Verify rejection with the harness NOT allowlisted**

The harness is served from the same origin as the doorbell page, which is **not** in the default allowlist — so this is the negative case, for free.

Run: `python server2.py`, open `http://localhost:8899/test-embed.html`

Expected:
- status shows `doorbell:ready (contract 1)`
- clicking any payload button changes **nothing**
- the iframe's console logs `ignoring doorbell:style from non-allowlisted origin: http://localhost:8899`

- [ ] **Step 3: Verify acceptance with the harness allowlisted**

Run: `DOORBELL_THEME_ORIGINS="http://localhost:8899" python server2.py`

Expected at startup: **no** insecure-origin warning. `http://localhost:8899` is a trusted origin (`localhost` over http is potentially trustworthy), so it passes `is_secure_origin`.

Open `http://localhost:8899/test-embed.html` and walk the checklist:

| # | Action | Expected |
|---|---|---|
| 1 | Colours only (dark) | recolours; layout unchanged |
| 2 | Colours only (warm paper) | recolours again — live switching, no reload |
| 3 | Layout: sidebar to bottom | sidebar becomes a bottom bar; **proves `layout` has no dead spot** |
| 4 | Layout: replies left, controls right | replies grid two-up, controls stack; **proves `replies`/`controls` are independent** |
| 5 | Fonts | all three faces change; no stray `font-family` wins |
| 6 | Clear override | returns to defaults exactly |
| 7 | Toggle scrim, then reload | **no light-grey flash** — the overlay is themed |
| 8 | Delete all replies in `messages.html`, reload | no "Quick Replies" heading floating over nothing; `data-doorbell-reply-count="0"` |
| 9 | Add replies back | count attribute updates on both `<body>` and `#messages` |

- [ ] **Step 3a: Verify the Chromecast path is untouched**

Run: `python server2.py` (no `DOORBELL_THEME_ORIGINS`), open `http://localhost:8899/webrtc-doorbell.html` directly.

Expected: pixel-identical to the pre-refactor page. To compare directly, extract the original to a scratch file and open it side by side — `0d7f697` is the last commit before implementation began:

```bash
git show 0d7f697:webrtc-doorbell.html > /tmp/doorbell-before.html
```

This is the acceptance criterion for the cast display, which receives no messages.

- [ ] **Step 4: Write the embedder-facing contract doc**

Create `docs/theming.md` documenting, for someone in another repo who cannot read this code: the protocol (`doorbell:ready` with `contract`, `doorbell:style`, `doorbell:video-playing`), the containment tree, the variable list, the state hooks, and the four rules that bite — **origins must be https**, `data-doorbell-reply="<slug>"` is cosmetic only, layout must not assume a reply count, and `data-doorbell-reply-count="0"` hides the heading by rule so anyone overriding `display` must re-handle the empty case. Source the content from the spec's *DOM contract* and *Protocol* sections; do not re-derive it.

Two things the doc must state that live nowhere else, both requested by the embedder:

**A. The variable table is definitive, with defaults.** Twelve rows — the nine colours and three fonts — each with its default value copied from the `:root` block in `webrtc-doorbell.html`. The defaults are not a fallback; they are what the Chromecast display and the HA card actually render, so they are part of the published product. An embedder must be able to build against this table without reconstructing it from conversation.

**B. Re-theming after a reload is the parent's job, and it is safe.** State plainly:

> `doorbell:ready` is a top-level statement in the page's script, so it fires on **every** initialisation — first load, refresh, `src` change, or cache-busting `?v=` bump alike. The page contains no self-reload path (`location.reload`, `location.href`, and `location.replace` appear nowhere in it), so a reload only ever happens because the embedder caused one.
>
> Therefore: **send your CSS in response to every `doorbell:ready`, not just the first.** A parent that themes only once will come back unthemed after any reload, and nothing will report the failure.

This closes the design's last silent failure mode, so it belongs in the doc rather than in a message thread.

- [ ] **Step 5: Note the environment variable in the README**

Add a `DOORBELL_THEME_ORIGINS` row to the existing `### Environment Variables` section (`README.md:36`): comma-separated origin allowlist for the theming channel, default `https://dashboard.baldino.me`, entries must be https (or localhost) or a parent frame there cannot host WebRTC at all.

Add a sentence to the existing `## WebRTC Doorbell Page` section (`README.md:97`) noting that the page is themeable by its embedder and linking to `docs/theming.md`.

- [ ] **Step 6: Commit**

```bash
git add test-embed.html docs/theming.md README.md
git commit -m "docs: theming contract reference and embedder harness"
```

---

## Post-implementation

Send `docs/theming.md` to the dashboard agent — it is the artefact it builds against, and it asked to review the contract as a list rather than discover gaps mid-build. Note explicitly that `test-embed.html` is a working reference parent it can copy the handshake from.

Two things remain outside this plan and should be flagged, not silently dropped:

1. **The kitchen tablet's origin is still unknown.** If it loads the dashboard over plain http, the doorbell iframe is not a secure context and the embed is already broken there, independent of this change. Cheapest check: the dashboard's Settings → Doorbell "Request Microphone Access" button — `getUserMedia` does not exist on an insecure origin, so if that has ever reported *granted* on the tablet itself, it is on a secure origin.
2. **Fonts need the dashboard's side shipped** — stable paths under `frontend/public/fonts/` and a CORS layer on its backend. Until then, `@font-face` payloads silently fall back. Task 6's font test uses generic families precisely so it does not depend on that.
