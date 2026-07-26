# Persistent Backchannel Session — Design

**Date:** 2026-07-26
**Component:** cam-proxy (`rtsp_proxy.py`)
**Status:** Approved design, pending implementation plan

## Problem

The doorbell popup on the kitchen dashboard shows live video but the talk button
produces no audio at the doorbell, while the dedicated cameras page — loading the
*identical* page, stream, and iframe permissions — works fine on the same tablet.

### Root cause (confirmed from logs)

The popup only ever appears on a doorbell ring, and a ring injects a ~4s chime
into the backchannel at the same moment go2rtc is connecting for the popup's talk
audio. The current `rtsp_proxy.py` design couples the upstream (camera) and
downstream (go2rtc) sessions into a single `_active_session`, and:

1. A new go2rtc client is **blocked** until the chime finishes
   (`_handle_client:217-219`).
2. During that wait, the prior session ends and nulls `_active_session`, so
   `await self._active_session.close()` at line 221 raises
   `AttributeError: 'NoneType' object has no attribute 'close'` — a check-then-act
   race across the `await`.
3. go2rtc then retries, opening multiple RTSP connections that **evict each other**
   (`closing previous session` → `upstream connection lost during PLAY`), producing
   a ~6s negotiation storm.

The talk audio leaves the browser fine (verified: `media-source` `audioLevel` > 0,
RTP packets sent) but has no stable backchannel session to ride, so it never
reaches the doorbell.

### Why this is architectural, not a one-line fix

The upstream camera connection is welded to go2rtc's downstream connection: when
go2rtc reconnects (which it does whenever a talk-capable consumer appears, to add
the backchannel track), the camera session is torn down and rebuilt from scratch.
The chime collides with that rebuild.

## Goals

- The camera/backchannel connection survives go2rtc reconnect churn.
- The chime always plays start-to-finish, regardless of consumer activity.
- The line-221 crash becomes structurally impossible.
- Keep the change scoped to `rtsp_proxy.py`; no changes to the browser page,
  go2rtc, or the dashboard app.

## Non-goals

- **Simultaneous multi-talker mixing** (multiple people talking into the doorbell
  at once, summed). That is a separate future effort ("option 2"); see below.
  This design leaves a clean seam for it but does not build it.
- Bypassing go2rtc for the talk path.

## Architecture

Split the single `_active_session` into three units with independent lifecycles.

```
                         ┌─────────── media (down) ───────────┐
                         │                                     ▼
  ┌──────────┐  RTSP    ┌───────────────┐   RTSP    ┌──────────┐
  │  go2rtc  │ ◄──────► │ ConsumerSession│ ◄──────► │CameraSession│ ◄─RTSP─► camera
  │(consumer)│          │  (transient)   │          │ (persistent)│         (all tracks)
  └──────────┘          └───────────────┘          └──────────┘
                               │  talk (up)               ▲
                               ▼                          │ emit()
                         ┌──────────────┐                 │
                         │  Backchannel  │────────────────┘
                         │ (owns audio   │◄── chime injection (server)
                         │  going UP)    │
                         └──────────────┘
```

### `CameraSession` (upstream, persistent)

- Owns cam-proxy's own RTSP connection to the camera: DESCRIBE / SETUP / PLAY on
  all tracks including the backchannel. Handles its own digest auth and
  reconnection with backoff.
- Exposes: live media frames (video/audio) coming down, and a single
  **`Backchannel`** for audio going up.
- Lifecycle is **keep-warm**: established on first demand (a consumer *or* a
  chime), stays alive through all consumer churn, and lingers ~30s (tunable)
  after the last consumer disconnects before tearing down. Absorbs the ring-time
  reconnect burst (~6s) without a 24/7 camera pull and without holding the
  doorbell's single backchannel open permanently.
- Names are generic (`Camera`, not `Doorbell`) — the proxy is a generic
  two-way-audio camera proxy; doorbell is one deployment.

### `Backchannel` (owns everything going up to the camera)

- The single owner of outbound RTP to the camera. Sources feed in — go2rtc's talk
  (via a `ConsumerSession`) and server-side chime injection — and it emits one
  stream to the camera through an isolated `emit()` / `combine()` seam.
- Option-1 policy: forward the one talk stream; **chime preempts** talk (chime
  plays, talk mutes briefly, then resumes) — matching today's behavior, no codec
  math.
- This is the seam that becomes `BackchannelMixer` in option 2 (see below).
  Nothing else writes to the camera, so mixing is a localized change here.

### `ConsumerSession` (downstream, transient)

- Spun up per downstream consumer connection (go2rtc today; named generically).
- Answers the consumer's negotiation from what `CameraSession` already knows — no
  re-negotiation with the camera. Relays live media out, and pipes the consumer's
  backchannel RTP into the `Backchannel`.
- On disconnect/reconnect, **only this unit is torn down**; `CameraSession` is
  untouched.
- The existing SETUP-dedup cache still applies within a `ConsumerSession`'s
  negotiation (go2rtc's sender-accumulation replays are harmless noise now).

### Consumer churn: fan-out, not eviction

`CameraSession` serves any number of `ConsumerSession`s from its one camera pull.
A new go2rtc connection is just a new `ConsumerSession`; the old one drops when
go2rtc closes it. **The "close the previous session" eviction path is removed
entirely**, and the line-221 crash with it — it cannot occur if nothing evicts.

## Data flow

- **Video/audio down:** camera → `CameraSession` → each `ConsumerSession` → consumer.
- **Talk up:** consumer → `ConsumerSession` → `Backchannel.emit()` → `CameraSession` → camera.
- **Chime up:** server → `Backchannel.emit()` → `CameraSession` → camera, on the
  persistent session, independent of any consumer.

## Edge cases

- **Camera drop (reboot/blip):** `CameraSession` reconnects with backoff while
  consumers are active or within keep-warm; consumers see a brief media gap.
- **Chime with no consumer:** plays instantly if `CameraSession` is warm;
  otherwise establishes it on demand (short connect delay) then plays.
- **Two talkers at once:** `Backchannel.emit()` forwards one (documented
  degradation), never crashes. Coherent mixing is option 2.
- **Chime during a consumer reconnect:** plays start-to-finish on the persistent
  session — the exact failing scenario from the log, now safe.

## Testing

The original bug was a race; the primary win is that the new structure makes it
unrepresentable. Still cover, building on the existing `test_dual_rtsp.py` harness:

- `CameraSession` stays up across `ConsumerSession` connect/disconnect (core invariant).
- A chime plays start-to-finish *through* a consumer reconnect.
- Keep-warm timer: session lingers then tears down; a chime after teardown
  re-establishes it.
- Camera-drop reconnect with an active consumer.

## Future: option 2 (multi-talker mixing)

Real mixing = decode each source (G.711 µ-law) to linear PCM, sum samples, clamp,
re-encode into one coherent RTP stream (single SSRC, monotonic seq/timestamp).
Interleaving packets from multiple sources does **not** work (incoherent
SSRC/seq/timestamp → garbage). This lands entirely inside `Backchannel`, which
graduates to `BackchannelMixer`. Feeding multiple *talkers* additionally requires
receiving them separately — i.e. a direct browser→cam-proxy talk path that
bypasses go2rtc, since go2rtc collapses talkers into one track before cam-proxy
sees them. Out of scope here; the persistent `CameraSession` + single-owner
`Backchannel` are the foundation it builds on.
