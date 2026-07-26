"""Persistent, self-driven RTSP session against the camera.

Unlike the current transparent-relay `ProxySession` (which mirrors whatever
go2rtc negotiates), `CameraSession` runs its OWN DESCRIBE/SETUP/PLAY
negotiation with the camera, independent of any downstream client. This lets
the camera connection persist across downstream (go2rtc) churn.

Because there's no downstream transport to mirror, `CameraSession` chooses
the interleaved channels itself: track1 -> 0-1, track2 -> 2-3, track3 -> 4-5,
etc, in SDP order. Media read from the camera is fanned out to registered
consumer callbacks; the sendonly (backchannel) track is wired into a single
owned `Backchannel` instance.
"""
import asyncio
import struct

from rtsp_wire import (
    AsyncRtspReader,
    build_rtsp_request,
    make_digest_auth,
    parse_auth_challenge,
)
from backchannel import Backchannel


# Reconnect backoff bounds (seconds).
_RECONNECT_BACKOFF_INITIAL = 1
_RECONNECT_BACKOFF_MAX = 30


class CameraSession:
    """Owns a persistent RTSP connection to the camera.

    Runs its own negotiation (DESCRIBE/SETUP/PLAY) and media pump, and
    reconnects with capped backoff on connection loss while `running`.
    """

    def __init__(self, host, port, user, password, stream):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.stream = stream

        self.sdp = ""
        self.tracks = {}
        self.running = False

        self.backchannel = Backchannel(send_frame=self._send_to_camera)

        self._consumers = set()
        self._write_lock = asyncio.Lock()
        self._writer = None
        self._reader = None
        self._task = None

        # Auth state for camera
        self._realm = ""
        self._nonce = ""

        # RTSP state
        self._cseq = 0
        self._session_id = None

    # -- consumer fan-out -------------------------------------------------

    def add_consumer(self, cb):
        self._consumers.add(cb)

    def remove_consumer(self, cb):
        self._consumers.discard(cb)

    async def _deliver(self, channel, payload):
        for cb in list(self._consumers):
            try:
                await cb(channel, payload)
            except Exception as e:
                # A misbehaving/disconnected consumer callback must never
                # be mistaken for a camera-pump error -- that would trip
                # _run_loop's reconnect path and churn the shared camera
                # connection for every other consumer. Log, drop the
                # offending consumer, and keep delivering to the rest.
                # (asyncio.CancelledError is a BaseException, not caught
                # here -- it propagates untouched, same as elsewhere in
                # this class.)
                print(f"CameraSession: consumer callback error, dropping consumer: {e}")
                self._consumers.discard(cb)

    # -- backchannel sink ---------------------------------------------------

    async def _send_to_camera(self, channel, payload):
        frame = struct.pack(">cBH", b"$", channel, len(payload)) + payload
        async with self._write_lock:
            # Check INSIDE the lock: _close_connection also takes this lock
            # before nulling self._writer, so the null-check and the write
            # are atomic w.r.t. each other -- a drop can't sneak in between
            # "writer looked non-None" and "writer.write() gets called".
            w = self._writer
            if w is None:
                return
            w.write(frame)
            await w.drain()

    # -- lifecycle ----------------------------------------------------------

    async def start(self):
        """Connect and run the first negotiation, then pump in the background.

        Awaits the first DESCRIBE/SETUP/PLAY round-trip so that `self.sdp`,
        `self.tracks`, and the backchannel channel are populated by the time
        this returns — consumers (Task 4's `ConsumerSession`) can rely on
        that. If the first negotiation fails, the exception propagates and
        `self.running` stays False; the caller finds out immediately rather
        than a retry-forever loop silently spinning with empty sdp/tracks.

        Once the first negotiation succeeds, a background task takes over:
        it pumps media and, if the connection later drops, reconnects with
        capped backoff and re-negotiates (repopulating sdp/tracks) — that
        recovery does not block this call.
        """
        if self.running:
            return
        try:
            await self._connect_and_negotiate()
        except Exception:
            # Don't leak a half-open socket on a failed first negotiation —
            # the doorbell only tolerates one backchannel connection cleanly,
            # so a dangling connection here can break the next attempt.
            # (asyncio.CancelledError is a BaseException, not caught here —
            # it propagates untouched, same as elsewhere in this class.)
            await self._close_connection()
            raise
        self.running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._close_connection()

    async def _run_loop(self):
        """Background: pump the (already-negotiated) connection.

        On drop, reconnect with capped backoff and re-negotiate before
        resuming the pump. Runs until `stop()` cancels it.
        """
        while self.running:
            try:
                await self._pump()
            except asyncio.CancelledError:
                raise
            except ConnectionError as e:
                print(f"CameraSession: connection error: {e}")
            except Exception as e:
                print(f"CameraSession: unexpected error: {e}")
            finally:
                await self._close_connection()

            if not self.running:
                break

            await self._reconnect_with_backoff()

    async def _reconnect_with_backoff(self):
        """Retry `_connect_and_negotiate` with capped exponential backoff
        until it succeeds or `stop()` clears `self.running`."""
        backoff = _RECONNECT_BACKOFF_INITIAL
        while self.running:
            print(f"CameraSession: reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            try:
                await self._connect_and_negotiate()
                return
            except asyncio.CancelledError:
                raise
            except ConnectionError as e:
                print(f"CameraSession: reconnect failed: {e}")
            except Exception as e:
                print(f"CameraSession: reconnect failed unexpectedly: {e}")
            await self._close_connection()
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

    async def _close_connection(self):
        # Hold the write lock across close+null so this can't race a
        # _send_to_camera write in flight (see _send_to_camera). Callers of
        # _close_connection never hold _write_lock themselves, so this
        # can't deadlock.
        async with self._write_lock:
            if self._writer:
                try:
                    self._writer.close()
                    await self._writer.wait_closed()
                except Exception:
                    pass
            self._writer = None
        self._reader = None

    # -- negotiation ----------------------------------------------------------

    def _url(self):
        return f"rtsp://{self.host}:{self.port}/{self.stream}"

    async def _connect_and_negotiate(self):
        """Connect to the camera and run DESCRIBE/SETUP(all tracks)/PLAY."""
        raw_reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._reader = AsyncRtspReader(raw_reader)

        # Fresh auth/session state for each (re)negotiation.
        self._realm = ""
        self._nonce = ""
        self._session_id = None

        await self._describe()
        await self._setup_all_tracks()
        await self._play()

    async def _next_cseq(self):
        self._cseq += 1
        return self._cseq

    async def _send_request(self, method, headers=None):
        """Send an RTSP request to the camera and return its response."""
        url = self._url()
        cseq = await self._next_cseq()
        us_headers = dict(headers or {})
        if self._session_id:
            us_headers.setdefault("Session", self._session_id)
        if self._realm and self._nonce:
            us_headers["Authorization"] = make_digest_auth(
                method, url, self.user, self.password, self._realm, self._nonce
            )
        self._writer.write(build_rtsp_request(method, url, cseq, us_headers))
        await self._writer.drain()
        return await self._read_response()

    async def _read_response(self):
        while True:
            item = await self._reader.read_frame_or_message()
            if item[0] == "interleaved":
                continue  # shouldn't happen before PLAY, but be safe
            _, first_line, headers, body = item
            status_code = int(first_line.split(" ")[1])
            return status_code, headers, body

    async def _describe(self):
        """DESCRIBE with ONVIF backchannel Require header + digest retry."""
        headers = {
            "Accept": "application/sdp",
            "Require": "www.onvif.org/ver20/backchannel",
        }
        status, resp_headers, resp_body = await self._send_request("DESCRIBE", headers)

        if status == 401:
            www_auth = resp_headers.get("www-authenticate", "")
            self._realm, self._nonce = parse_auth_challenge(www_auth)
            status, resp_headers, resp_body = await self._send_request("DESCRIBE", headers)

        if status != 200:
            raise ConnectionError(f"DESCRIBE failed: {status}")

        self.sdp = resp_body.decode("utf-8", errors="replace") if resp_body else ""
        self._parse_sdp_tracks(self.sdp)

    async def _setup_all_tracks(self):
        """SETUP each SDP track, assigning interleaved channels in SDP order.

        track1 -> 0-1, track2 -> 2-3, track3 -> 4-5, etc (2 channels/track,
        RTP then RTCP). On the sendonly track, wires the backchannel's
        upstream channel.
        """
        for index, (track_id, info) in enumerate(self.tracks.items()):
            rtp_ch = index * 2
            rtcp_ch = rtp_ch + 1
            url = f"{self._url()}/{track_id}"
            headers = {"Transport": f"RTP/AVP/TCP;unicast;interleaved={rtp_ch}-{rtcp_ch}"}

            status, resp_headers, _ = await self._send_request("SETUP", headers)

            if status != 200:
                raise ConnectionError(f"SETUP {track_id} failed: {status}")

            if not self._session_id and "session" in resp_headers:
                self._session_id = resp_headers["session"].split(";")[0]

            info["rtp_channel"] = rtp_ch
            info["rtcp_channel"] = rtcp_ch

            print(
                f"CameraSession: SETUP {track_id}: us ch {rtp_ch}-{rtcp_ch} "
                f"({info.get('direction', '?')})"
            )

            if info.get("direction") == "sendonly":
                self.backchannel.set_channel(rtp_ch)

    async def _play(self):
        headers = {"Range": "npt=0.000-"}
        status, _, _ = await self._send_request("PLAY", headers)
        if status != 200:
            raise ConnectionError(f"PLAY failed: {status}")
        print("CameraSession: negotiation complete, starting media pump")

    def _parse_sdp_tracks(self, sdp):
        """Parse SDP media sections into track descriptors keyed by control id.

        Ported from ProxySession._parse_sdp_tracks.
        """
        tracks = {}
        current = None
        for line in sdp.strip().split("\n"):
            line = line.strip().rstrip("\r")
            if line.startswith("m="):
                if current and "control" in current:
                    tracks[current["control"]] = current
                parts = line.split()
                current = {"kind": parts[0][2:], "direction": "recvonly"}
            elif current:
                if line.startswith("a=control:"):
                    current["control"] = line.split(":", 1)[1]
                elif line == "a=sendonly":
                    current["direction"] = "sendonly"
                elif line == "a=recvonly":
                    current["direction"] = "recvonly"
        if current and "control" in current:
            tracks[current["control"]] = current

        self.tracks = tracks
        for ctrl, info in self.tracks.items():
            print(f"CameraSession: SDP track {ctrl}: {info['kind']} {info['direction']}")

    # -- media pump -----------------------------------------------------------

    async def _pump(self):
        """Read interleaved frames from the camera and fan them out.

        Delivers every interleaved channel (RTP and RTCP) — filtering by
        track/direction is a consumer concern, not the pump's.
        """
        while self.running:
            item = await self._reader.read_frame_or_message()
            if item[0] == "interleaved":
                _, channel, payload = item
                await self._deliver(channel, payload)
            else:
                # Keepalive / async RTSP message from the camera during
                # streaming (e.g. OPTIONS). Nothing to do — we don't relay
                # camera-initiated requests anywhere.
                pass
