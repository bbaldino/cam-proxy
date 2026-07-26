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
            await cb(channel, payload)

    # -- backchannel sink ---------------------------------------------------

    async def _send_to_camera(self, channel, payload):
        if self._writer is None:
            return
        frame = struct.pack(">cBH", b"$", channel, len(payload)) + payload
        async with self._write_lock:
            self._writer.write(frame)
            await self._writer.drain()

    # -- lifecycle ----------------------------------------------------------

    async def start(self):
        """Start the session task: negotiate, pump media, reconnect on drop."""
        if self.running:
            return
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
        """Reconnect-with-backoff loop: connect, negotiate, pump; repeat on drop."""
        backoff = _RECONNECT_BACKOFF_INITIAL
        while self.running:
            try:
                await self._connect_and_negotiate()
                backoff = _RECONNECT_BACKOFF_INITIAL  # reset after a clean negotiation
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

            print(f"CameraSession: reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

    async def _close_connection(self):
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
