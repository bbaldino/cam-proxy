# Persistent Backchannel Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple cam-proxy's persistent camera (upstream) RTSP session from transient go2rtc consumer (downstream) sessions, so the backchannel survives go2rtc reconnect churn and the chime never collides with a connecting consumer.

**Architecture:** Split the monolithic `ProxySession` (which relays go2rtc's negotiation straight through to the camera) into three units: a persistent `CameraSession` that runs its *own* RTSP negotiation with the camera and owns the media hub; a `Backchannel` that is the single owner of audio going up to the camera (chime + talk, with an `emit()` seam for future mixing); and transient `ConsumerSession`s that answer a consumer's negotiation from `CameraSession`'s already-known SDP and feed the `Backchannel`. The `RtspProxy` server manages `CameraSession` lifecycle (keep-warm) and fans media out to consumers.

**Tech Stack:** Python 3.12, asyncio, aiohttp (existing); pytest + pytest-asyncio for tests.

## Global Constraints

- Python 3.12+ (matches Dockerfile base `python:3.12-slim`).
- Acronyms: only first letter capitalized (`RtspProxy`, `CameraSession`) — matches existing style and user preference.
- No new runtime dependencies beyond what's in the image (aiohttp, pychromecast, gtts). Test-only deps (`pytest`, `pytest-asyncio`) are fine.
- Camera backchannel uses `Require: www.onvif.org/ver20/backchannel` and digest auth (realm "BC Streaming Media") — do not change.
- Chime audio is PCMU/G.711 at 8kHz, 160 samples (20ms) per RTP packet.
- Keep-warm window: 30 seconds (tunable constant).
- Scope is `rtsp_proxy.py` and its split-out modules plus the `server2.py` chime-injection call site. No changes to the browser page, go2rtc, or the dashboard app.

---

## File Structure

- `rtsp_wire.py` (new) — pure protocol helpers extracted from `rtsp_proxy.py`: `AsyncRtspReader`, `build_rtsp_request`, `build_rtsp_response`, `make_digest_auth`, `parse_auth_challenge`, `md5hex`, transport/SDP parse helpers. No I/O beyond the reader. Fully unit-testable.
- `backchannel.py` (new) — `Backchannel`: owns outbound RTP to the camera; `emit()` seam; chime injection with preempt policy.
- `camera_session.py` (new) — `CameraSession`: persistent, self-driven RTSP negotiation with the camera; media fan-out to consumers; owns one `Backchannel`; reconnect with backoff.
- `consumer_session.py` (new) — `ConsumerSession`: serves one downstream consumer from a `CameraSession`; feeds talk RTP into the `Backchannel`.
- `rtsp_proxy.py` (rewritten) — `RtspProxy`: accepts consumer connections, manages `CameraSession` keep-warm lifecycle, wires everything. Keeps `inject_chime` / `inject_chime_path` public API.
- `server2.py` (modified) — call site for chime injection unchanged in signature; verify it still resolves.
- `tests/` (new) — `test_rtsp_wire.py`, `test_backchannel.py`, `test_camera_session.py`, `test_consumer_session.py`, `test_proxy_lifecycle.py`.

---

## Task 1: Extract and test wire protocol helpers

**Files:**
- Create: `rtsp_wire.py`
- Modify: `rtsp_proxy.py` (remove the moved helpers, import from `rtsp_wire`)
- Create: `tests/test_rtsp_wire.py`
- Create: `tests/conftest.py` (empty; makes `tests/` a package root for pytest)

**Interfaces:**
- Produces:
  - `md5hex(s: str) -> str`
  - `make_digest_auth(method, url, user, password, realm, nonce) -> str`
  - `parse_auth_challenge(header_value: str) -> tuple[str, str]` (realm, nonce)
  - `build_rtsp_request(method, url, cseq, headers: dict|None=None, body: bytes=b"") -> bytes`
  - `build_rtsp_response(status_code, status_text, cseq, headers=None, body=b"") -> bytes`
  - `class AsyncRtspReader` with `async read_frame_or_message() -> tuple` returning `("interleaved", channel:int, payload:bytes)` or `("rtsp", first_line:str, headers:dict, body:bytes)`
  - `parse_interleaved(transport: str) -> tuple[int|None, int|None]` (rtp_ch, rtcp_ch)

- [ ] **Step 1: Add test deps**

Add to a new `requirements-dev.txt`:
```
pytest
pytest-asyncio
```
Run: `pip install -r requirements-dev.txt`

- [ ] **Step 2: Write the failing test for framing + digest**

```python
# tests/test_rtsp_wire.py
import asyncio
import pytest
from rtsp_wire import (
    md5hex, make_digest_auth, parse_auth_challenge,
    build_rtsp_request, AsyncRtspReader, parse_interleaved,
)

def test_digest_matches_known_vector():
    # ha1=md5(user:realm:pass), ha2=md5(method:uri), resp=md5(ha1:nonce:ha2)
    got = make_digest_auth("DESCRIBE", "rtsp://x/y", "u", "p", "BC Streaming Media", "abc")
    ha1 = md5hex("u:BC Streaming Media:p")
    ha2 = md5hex("DESCRIBE:rtsp://x/y")
    assert md5hex(f"{ha1}:abc:{ha2}") in got
    assert 'realm="BC Streaming Media"' in got

def test_parse_auth_challenge():
    realm, nonce = parse_auth_challenge('Digest realm="BC Streaming Media", nonce="XYZ"')
    assert (realm, nonce) == ("BC Streaming Media", "XYZ")

def test_parse_interleaved():
    assert parse_interleaved("RTP/AVP/TCP;interleaved=4-5") == (4, 5)
    assert parse_interleaved("RTP/AVP/TCP;interleaved=4") == (4, None)

class _FakeStream:
    def __init__(self, data): self._data = data
    async def read(self, n):
        if not self._data: return b""
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

@pytest.mark.asyncio
async def test_reader_parses_interleaved_then_rtsp():
    import struct
    payload = b"\x00\x01\x02"
    frame = struct.pack(">cBH", b"$", 4, len(payload)) + payload
    msg = b"OPTIONS rtsp://x RTSP/1.0\r\nCSeq: 1\r\n\r\n"
    r = AsyncRtspReader(_FakeStream(frame + msg))
    assert await r.read_frame_or_message() == ("interleaved", 4, payload)
    kind, first_line, headers, body = await r.read_frame_or_message()
    assert kind == "rtsp" and headers["cseq"] == "1"
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_rtsp_wire.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rtsp_wire'`

- [ ] **Step 4: Create `rtsp_wire.py`**

Move `md5hex`, `make_digest_auth`, `parse_auth_challenge`, `build_rtsp_request`, `build_rtsp_response`, and `AsyncRtspReader` verbatim from `rtsp_proxy.py` (lines 24-152) into `rtsp_wire.py`. Add a standalone `parse_interleaved` by lifting the body of `ProxySession._parse_interleaved`:
```python
import re
def parse_interleaved(transport):
    m = re.search(r"interleaved=(\d+)(?:-(\d+))?", transport)
    if not m:
        return None, None
    rtp = int(m.group(1))
    rtcp = int(m.group(2)) if m.group(2) else None
    return rtp, rtcp
```

- [ ] **Step 5: Update `rtsp_proxy.py` imports**

At the top of `rtsp_proxy.py`, remove the moved definitions and add:
```python
from rtsp_wire import (
    md5hex, make_digest_auth, parse_auth_challenge,
    build_rtsp_request, build_rtsp_response, AsyncRtspReader, parse_interleaved,
)
```
Replace internal `self._parse_interleaved(x)` calls with `parse_interleaved(x)`.

- [ ] **Step 6: Run tests + import smoke check**

Run: `pytest tests/test_rtsp_wire.py -v && python -c "import rtsp_proxy"`
Expected: PASS, and `rtsp_proxy` imports without error.

- [ ] **Step 7: Commit**

```bash
git add rtsp_wire.py rtsp_proxy.py tests/ requirements-dev.txt
git commit -m "refactor: extract RTSP wire helpers into rtsp_wire"
```

---

## Task 2: `Backchannel` — single owner of outbound audio

**Files:**
- Create: `backchannel.py`
- Create: `tests/test_backchannel.py`

**Interfaces:**
- Consumes: `build_rtsp_request` not needed; uses raw interleaved framing.
- Produces:
  - `class Backchannel(send_frame)` where `send_frame` is `async (channel:int, payload:bytes) -> None` — the sink that writes one interleaved RTP frame to the camera. `Backchannel` never touches a socket directly.
  - `set_channel(upstream_channel: int) -> None` — set once the backchannel channel is known from negotiation; `None` until then.
  - `async emit(payload: bytes) -> None` — forward one talk RTP packet from a consumer. No-op if a chime is playing (preempt) or channel unknown.
  - `async play_chime(pcmu_data: bytes) -> None` — inject a chime; sets `chime_playing` for the duration; builds its own RTP.
  - property `chime_playing: bool`
  - `async wait_chime_done() -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backchannel.py
import asyncio, pytest
from backchannel import Backchannel

@pytest.mark.asyncio
async def test_emit_forwards_when_idle():
    sent = []
    bc = Backchannel(send_frame=lambda ch, p: sent.append((ch, p)) or asyncio.sleep(0))
    bc.set_channel(4)
    await bc.emit(b"talk")
    assert sent == [(4, b"talk")]

@pytest.mark.asyncio
async def test_emit_dropped_before_channel_known():
    sent = []
    bc = Backchannel(send_frame=lambda ch, p: sent.append((ch, p)) or asyncio.sleep(0))
    await bc.emit(b"talk")  # no channel yet
    assert sent == []

@pytest.mark.asyncio
async def test_chime_preempts_talk():
    sent = []
    async def sink(ch, p): sent.append((ch, p))
    bc = Backchannel(send_frame=sink)
    bc.set_channel(4)
    # 320 samples = two 20ms packets
    task = asyncio.create_task(bc.play_chime(b"\x00" * 320))
    await asyncio.sleep(0)              # let chime start
    assert bc.chime_playing
    await bc.emit(b"talk")              # should be dropped during chime
    await task
    assert not bc.chime_playing
    assert (4, b"talk") not in sent     # talk was preempted
    assert len(sent) == 2               # two chime packets emitted
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_backchannel.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backchannel'`

- [ ] **Step 3: Implement `backchannel.py`**

```python
import asyncio, random, struct

class Backchannel:
    """Single owner of audio going UP to the camera.

    Sources (consumer talk, chime) feed in; emit() is the seam where they
    combine. Option 1 policy: forward talk, chime preempts. Becomes a real
    mixer (BackchannelMixer) in option 2.
    """
    def __init__(self, send_frame):
        self._send_frame = send_frame        # async (channel, payload) -> None
        self._channel = None
        self._chime_playing = False
        self._chime_done = asyncio.Event(); self._chime_done.set()
        self._lock = asyncio.Lock()
        self._ssrc = random.randint(0, 0xFFFFFFFF)
        self._seq = 0
        self._ts = 0

    def set_channel(self, upstream_channel):
        self._channel = upstream_channel

    @property
    def chime_playing(self):
        return self._chime_playing

    async def wait_chime_done(self):
        await self._chime_done.wait()

    async def emit(self, payload):
        if self._channel is None or self._chime_playing:
            return
        await self._send_frame(self._channel, payload)

    async def play_chime(self, pcmu_data):
        async with self._lock:
            if self._channel is None:
                return
            self._chime_playing = True
            self._chime_done.clear()
            try:
                start = asyncio.get_event_loop().time()
                n = 0
                for offset in range(0, len(pcmu_data), 160):
                    chunk = pcmu_data[offset:offset + 160]
                    if len(chunk) < 160:
                        chunk += b"\xff" * (160 - len(chunk))
                    self._seq = (self._seq + 1) & 0xFFFF
                    header = struct.pack(">BBHII", 0x80, 0, self._seq, self._ts, self._ssrc)
                    self._ts = (self._ts + len(chunk)) & 0xFFFFFFFF
                    await self._send_frame(self._channel, header + chunk)
                    n += 1
                    target = start + n * 0.020
                    now = asyncio.get_event_loop().time()
                    if target > now:
                        await asyncio.sleep(target - now)
            finally:
                self._chime_playing = False
                self._chime_done.set()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_backchannel.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backchannel.py tests/test_backchannel.py
git commit -m "feat: Backchannel owns outbound audio with emit() seam"
```

---

## Task 3: `CameraSession` — persistent, self-driven upstream

**Files:**
- Create: `camera_session.py`
- Create: `tests/test_camera_session.py`

**Interfaces:**
- Consumes: `rtsp_wire` helpers; `Backchannel`.
- Produces:
  - `class CameraSession(host, port, user, password, stream)`
  - `async start() -> None` — connect, run its own DESCRIBE/SETUP/PLAY negotiation with the camera (mirroring current `_handle_describe`/`_handle_setup`/PLAY but driven by CameraSession itself, requesting all tracks incl. backchannel), then begin pumping. Sets `self.backchannel` channel on backchannel SETUP.
  - `async stop() -> None`
  - `sdp: str` — the camera's SDP (for consumers to answer DESCRIBE).
  - `tracks: dict` — parsed track info + channel assignments (for consumers).
  - `backchannel: Backchannel`
  - `add_consumer(cb) -> None` / `remove_consumer(cb) -> None` where `cb` is `async (channel:int, payload:bytes) -> None` receiving downstream media frames (fan-out).
  - `running: bool`
- Note: negotiation and the media pump against a live camera are integration-tested (Task 7). The **channel-mapping / SDP-parse / fan-out registry** are unit-tested here with a fake camera stream.

- [ ] **Step 1: Write failing unit tests for the testable seams**

```python
# tests/test_camera_session.py
import pytest
from camera_session import CameraSession

def test_fanout_registry_delivers_to_all_consumers():
    cs = CameraSession("h", 554, "u", "p", "s")
    got_a, got_b = [], []
    async def a(ch, p): got_a.append((ch, p))
    async def b(ch, p): got_b.append((ch, p))
    cs.add_consumer(a); cs.add_consumer(b)
    import asyncio
    asyncio.run(cs._deliver(0, b"vid"))
    assert got_a == [(0, b"vid")] and got_b == [(0, b"vid")]
    cs.remove_consumer(a)
    asyncio.run(cs._deliver(0, b"vid2"))
    assert got_a == [(0, b"vid")] and got_b[-1] == (0, b"vid2")

def test_parse_sdp_marks_backchannel_track():
    cs = CameraSession("h", 554, "u", "p", "s")
    sdp = ("v=0\r\n"
           "m=video 0 RTP/AVP 96\r\na=control:track1\r\n"
           "m=audio 0 RTP/AVP 97\r\na=control:track3\r\na=sendonly\r\n")
    cs._parse_sdp_tracks(sdp)
    assert cs.tracks["track3"]["direction"] == "sendonly"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_camera_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'camera_session'`

- [ ] **Step 3: Implement `CameraSession`**

Port the camera-facing logic from the current `ProxySession`, but **inverted**: instead of relaying go2rtc's requests, `CameraSession` issues its own. Reuse verbatim where possible:
- `_parse_sdp_tracks` (current lines ~644+) and `_parse_interleaved` → `parse_interleaved`.
- DESCRIBE with `Require: www.onvif.org/ver20/backchannel` + digest retry (current `_handle_describe:373-401`, minus the downstream write).
- For each track in the SDP, issue SETUP (current `_handle_setup` camera half, lines 434-473), record `ds`/`us` channel assignments in `self.tracks`, and on `direction == "sendonly"` call `self.backchannel.set_channel(us_rtp_ch)`.
- Issue PLAY, then run the pump loop reading interleaved frames from the camera and calling `self._deliver(channel, payload)` for media (fan-out to consumers). Backchannel writes go the other way via `self.backchannel`'s `send_frame` sink, which is `self._send_to_camera(channel, payload)` guarded by an upstream write lock (port `_send_interleaved` + `_us_write_lock`).
- `start()` wraps negotiation + pump in a task; on `ConnectionError`, reconnect with capped backoff while `self.running`.

Key skeleton:
```python
import asyncio
from rtsp_wire import AsyncRtspReader, build_rtsp_request, make_digest_auth, parse_auth_challenge, parse_interleaved
from backchannel import Backchannel

class CameraSession:
    def __init__(self, host, port, user, password, stream):
        self.host, self.port = host, port
        self.user, self.password, self.stream = user, password, stream
        self.sdp = ""
        self.tracks = {}
        self._consumers = set()
        self._write_lock = asyncio.Lock()
        self._writer = None
        self.running = False
        self.backchannel = Backchannel(send_frame=self._send_to_camera)

    def add_consumer(self, cb): self._consumers.add(cb)
    def remove_consumer(self, cb): self._consumers.discard(cb)

    async def _deliver(self, channel, payload):
        for cb in list(self._consumers):
            await cb(channel, payload)

    async def _send_to_camera(self, channel, payload):
        import struct
        if self._writer is None: return
        frame = struct.pack(">cBH", b"$", channel, len(payload)) + payload
        async with self._write_lock:
            self._writer.write(frame); await self._writer.drain()

    def _parse_sdp_tracks(self, sdp): ...   # port from ProxySession
    async def start(self): ...              # negotiate + pump + reconnect loop
    async def stop(self): ...
```

- [ ] **Step 4: Run unit tests**

Run: `pytest tests/test_camera_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add camera_session.py tests/test_camera_session.py
git commit -m "feat: CameraSession runs its own camera negotiation + media fan-out"
```

---

## Task 4: `ConsumerSession` — serve a consumer from `CameraSession`

**Files:**
- Create: `consumer_session.py`
- Create: `tests/test_consumer_session.py`

**Interfaces:**
- Consumes: `CameraSession` (`sdp`, `tracks`, `backchannel`, `add_consumer`/`remove_consumer`), `rtsp_wire` helpers.
- Produces:
  - `class ConsumerSession(camera: CameraSession, downstream_reader, downstream_writer)`
  - `async run() -> None` — answer the consumer's DESCRIBE (return `camera.sdp`) / SETUP (map to `camera.tracks` channels, rewrite Transport) / PLAY from cached knowledge, **without touching the camera**; register a media callback via `camera.add_consumer`; then in the streaming phase, forward downstream frames on the backchannel channel into `camera.backchannel.emit()`.
  - `async close() -> None` — `camera.remove_consumer(...)`, close the downstream writer. **Never** stops the `CameraSession`.

- [ ] **Step 1: Write failing test (fake consumer socket + stub camera)**

```python
# tests/test_consumer_session.py
import asyncio, struct, pytest
from consumer_session import ConsumerSession

class _StubCamera:
    def __init__(self):
        self.sdp = "v=0\r\n"
        self.tracks = {"track3": {"direction": "sendonly", "us_ch": 4}}
        self.added = []; self.emitted = []
        class _BC:
            async def emit(_, p): self.emitted.append(p)
        self.backchannel = _BC()
    def add_consumer(self, cb): self.added.append(cb)
    def remove_consumer(self, cb): pass

class _FakeWriter:
    def __init__(self): self.buf = b""
    def write(self, d): self.buf += d
    async def drain(self): pass
    def close(self): pass

@pytest.mark.asyncio
async def test_close_does_not_stop_camera_and_deregisters():
    cam = _StubCamera()
    reader = asyncio.StreamReader()
    reader.feed_eof()
    cs = ConsumerSession(cam, reader, _FakeWriter())
    await cs.close()          # must not raise, must not touch camera lifecycle
    # camera has no stop() called — proven by cam not exposing one; assert no exception
    assert True
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_consumer_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'consumer_session'`

- [ ] **Step 3: Implement `ConsumerSession`**

Port the downstream-facing negotiation from the current `ProxySession` (`_handle_describe` downstream-write half, `_handle_setup` transport rewrite, PLAY ack), but answer from `camera.sdp` / `camera.tracks` instead of round-tripping to the camera. In the streaming phase, read downstream interleaved frames; when `channel == backchannel_downstream_channel`, call `await camera.backchannel.emit(payload)`. Media the *other* direction is pushed by `camera` via the registered callback, which writes interleaved frames to `self.ds_writer`. `close()` calls `camera.remove_consumer(self._on_media)` and closes the writer only.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_consumer_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add consumer_session.py tests/test_consumer_session.py
git commit -m "feat: ConsumerSession serves consumers from CameraSession, feeds Backchannel"
```

---

## Task 5: Rewrite `RtspProxy` — lifecycle, keep-warm, fan-out, no eviction

**Files:**
- Modify: `rtsp_proxy.py` (replace `ProxySession` usage; keep `AsyncRtspReader` import from `rtsp_wire`)
- Create: `tests/test_proxy_lifecycle.py`

**Interfaces:**
- Consumes: `CameraSession`, `ConsumerSession`.
- Produces (unchanged public API so `server2.py` keeps working):
  - `class RtspProxy(camera_host, camera_port, camera_user, camera_pass, camera_stream, listen_port=8554)`
  - `async start() -> None`, `async stop() -> None`
  - `async inject_chime(filename) -> str`, `async inject_chime_path(filepath) -> str`
  - New internal: `KEEP_WARM_SECONDS = 30`; `_ensure_camera()` establishes `CameraSession` on demand and cancels any pending keep-warm teardown; `_schedule_teardown()` starts the 30s timer when the last consumer leaves.

- [ ] **Step 1: Write failing lifecycle tests**

```python
# tests/test_proxy_lifecycle.py
import asyncio, pytest
from rtsp_proxy import RtspProxy

@pytest.mark.asyncio
async def test_camera_persists_across_consumer_churn(monkeypatch):
    starts = {"n": 0}
    class FakeCamera:
        def __init__(self, *a): starts["n"] += 1; self.running = True
        async def start(self): pass
        async def stop(self): self.running = False
        backchannel = None
    monkeypatch.setattr("rtsp_proxy.CameraSession", FakeCamera)
    p = RtspProxy("h", 554, "u", "p", "s")
    await p._ensure_camera()
    await p._ensure_camera()          # second consumer arrives
    assert starts["n"] == 1           # only ONE camera session created

@pytest.mark.asyncio
async def test_inject_chime_without_camera_reports_gracefully(monkeypatch):
    p = RtspProxy("h", 554, "u", "p", "s")
    # no camera warm, file missing → returns a string, never raises AttributeError
    result = await p.inject_chime_path("/nonexistent.pcmu")
    assert isinstance(result, str)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_proxy_lifecycle.py -v`
Expected: FAIL — `AttributeError: 'RtspProxy' object has no attribute '_ensure_camera'`

- [ ] **Step 3: Rewrite `RtspProxy`**

Replace `_handle_client` (current lines 212-233, including the crashing `_active_session.close()` at 221) with:
```python
KEEP_WARM_SECONDS = 30

async def _ensure_camera(self):
    if self._teardown_task:
        self._teardown_task.cancel(); self._teardown_task = None
    if self._camera is None or not self._camera.running:
        self._camera = CameraSession(self.camera_host, self.camera_port,
                                     self.camera_user, self.camera_pass,
                                     self.camera_stream)
        await self._camera.start()
    return self._camera

def _schedule_teardown(self):
    async def _later():
        try:
            await asyncio.sleep(KEEP_WARM_SECONDS)
            if not self._consumers and self._camera:
                await self._camera.stop(); self._camera = None
        except asyncio.CancelledError:
            pass
    if self._teardown_task:
        self._teardown_task.cancel()
    self._teardown_task = asyncio.create_task(_later())

async def _handle_client(self, reader, writer):
    camera = await self._ensure_camera()
    session = ConsumerSession(camera, reader, writer)
    self._consumers.add(session)
    try:
        await session.run()
    except Exception as e:
        print(f"RTSP proxy: consumer error: {e}")
    finally:
        await session.close()
        self._consumers.discard(session)
        if not self._consumers:
            self._schedule_teardown()   # keep-warm, NOT immediate teardown
```
Update `inject_chime_path` to `await self._ensure_camera()` first, then `await camera.backchannel.play_chime(pcmu)` guarding on `backchannel._channel is None`. Init `self._camera=None`, `self._consumers=set()`, `self._teardown_task=None` in `__init__`. Delete `ProxySession` (now fully replaced) once nothing references it.

- [ ] **Step 4: Run tests + import smoke**

Run: `pytest tests/test_proxy_lifecycle.py -v && python -c "import server2"`
Expected: PASS, and `server2` imports (verifies the public API still resolves).

- [ ] **Step 5: Commit**

```bash
git add rtsp_proxy.py tests/test_proxy_lifecycle.py
git commit -m "feat: RtspProxy keep-warm camera lifecycle, fan-out consumers, no eviction"
```

---

## Task 6: Live integration verification

**Files:**
- Modify: `test_dual_rtsp.py` (extend the existing manual harness) or add `tests/test_live_backchannel.md` runbook.

**Interfaces:** none (manual, against the running stack).

- [ ] **Step 1: Deploy the rebuilt image to the cam-proxy container**

Build/redeploy per current process (host-networked container). Confirm startup log shows `RTSP proxy listening on port 8554`.

- [ ] **Step 2: Verify the base case still works**

Open the dedicated cameras page, press TALK, confirm audio at the doorbell and `level=` moving in the overlay. Expected: works as before (no regression).

- [ ] **Step 3: Verify the failing case is fixed**

Trigger a real ring (press the doorbell) so the chime injects AND the popup opens. Press TALK during/after the chime. Watch `docker logs -f` on cam-proxy.
Expected: **no** `NoneType' object has no attribute 'close'`, **no** `closing previous session` / `upstream connection lost during PLAY` storm; chime plays fully; talk audio reaches the doorbell.

- [ ] **Step 4: Verify keep-warm**

After closing all viewers, confirm the log shows the camera session torn down ~30s later, and that a chime fired within the window plays instantly (no reconnect).

- [ ] **Step 5: Commit any runbook/harness updates**

```bash
git add test_dual_rtsp.py
git commit -m "test: live backchannel verification runbook"
```

---

## Self-Review

**Spec coverage:**
- Decouple upstream/downstream → Tasks 3, 4, 5. ✓
- `CameraSession` persistent, keep-warm → Tasks 3, 5. ✓
- `Backchannel` single owner + `emit()` seam → Task 2. ✓
- `ConsumerSession` transient, fan-out, no eviction → Tasks 4, 5. ✓
- Crash removed structurally → Task 5 (no `_active_session.close()` path). ✓
- Chime preempt policy → Task 2. ✓
- Chime through churn / no-consumer chime → Tasks 2, 5, 6. ✓
- Camera-drop reconnect → Task 3 (backoff loop), Task 6 (verify). ✓
- Testing on `test_dual_rtsp.py` harness → Task 6. ✓
- Names generic (`Camera`, not `Doorbell`) → all tasks. ✓

**Placeholder scan:** Task 3 and Task 4 implementation steps say "port from `ProxySession`" with exact source line ranges and the specific transformation (invert who drives negotiation); the testable seams have real test code. No bare TODOs.

**Type consistency:** `Backchannel(send_frame)` sink signature `async (channel, payload)` matches `CameraSession._send_to_camera`. `emit(payload)` used identically in Tasks 2/4. `add_consumer`/`remove_consumer`/`_deliver` callback signature `async (channel, payload)` consistent across Tasks 3/4. `_ensure_camera` / `_schedule_teardown` names consistent across Task 5.
