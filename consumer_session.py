"""Transient downstream half: serves one consumer (go2rtc) from a CameraSession.

Answers the consumer's DESCRIBE/SETUP/PLAY entirely from what `CameraSession`
already knows (`camera.sdp`, `camera.tracks`) -- it never round-trips to the
camera during negotiation. Once streaming, camera media is pushed to this
consumer via a callback registered with `camera.add_consumer`; RTP the
consumer sends on its backchannel (talk) channel is forwarded into
`camera.backchannel.emit()`.

`camera.sdp`/`camera.tracks` are snapshotted once, at the start of `run()`,
rather than re-read later. `CameraSession.tracks` can be observed
mid-reconnect in a partially-populated state (fields filled in across awaits
during a later camera reconnect); reading it once up front means a
reconnect happening concurrently can't corrupt this consumer's already
negotiated channel mapping.

`close()` deregisters from the camera and closes only the downstream
writer -- it must never stop the `CameraSession`, which is shared by other
consumers and outlives any single one of them.
"""
import asyncio
import struct
import uuid

from rtsp_wire import AsyncRtspReader, build_rtsp_response, parse_interleaved


class ConsumerSession:
    """Serves one downstream consumer (go2rtc) from an already-running CameraSession."""

    def __init__(self, camera, downstream_reader, downstream_writer):
        self.camera = camera
        self.ds_reader = AsyncRtspReader(downstream_reader)
        self.ds_writer = downstream_writer

        # Snapshot of camera.sdp/camera.tracks -- taken once, in run(). See
        # module docstring for why this isn't read again after that.
        self._sdp = ""
        self._tracks = {}

        self._session_id = uuid.uuid4().hex[:8]

        # Camera channel -> consumer channel, built during SETUP: the
        # consumer picks its own interleaved channel numbers, independent
        # of the camera's, so media pushed from the camera has to be
        # translated before it goes out.
        self._cam_to_ds_channel = {}
        self._bc_ds_channel = None  # consumer's channel for the sendonly track

        self._closed = False
        self._ds_write_lock = asyncio.Lock()

    async def run(self):
        """Negotiate with the consumer, then relay media until it disconnects."""
        self._sdp = self.camera.sdp
        self._tracks = dict(self.camera.tracks)

        played = await self._negotiate()
        if not played:
            return

        self.camera.add_consumer(self._on_media)
        try:
            await self._stream_downstream()
        except (asyncio.CancelledError, ConnectionError):
            pass

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self.camera.remove_consumer(self._on_media)
        try:
            self.ds_writer.close()
            wait_closed = getattr(self.ds_writer, "wait_closed", None)
            if wait_closed:
                await wait_closed()
        except Exception:
            pass

    # -- negotiation (answered from the snapshot, no camera round-trip) -----

    async def _negotiate(self):
        while not self._closed:
            try:
                item = await self.ds_reader.read_frame_or_message()
            except ConnectionError:
                return False

            if item[0] == "interleaved":
                continue  # shouldn't happen before PLAY, but be safe

            _, first_line, headers, _body = item
            parts = first_line.split(" ", 2)
            if len(parts) < 2:
                continue

            method = parts[0]
            url = parts[1]
            cseq = headers.get("cseq", "1")

            if method == "DESCRIBE":
                await self._handle_describe(cseq)
            elif method == "SETUP":
                await self._handle_setup(url, cseq, headers)
            elif method == "PLAY":
                await self._respond_ok(cseq)
                return True
            elif method == "TEARDOWN":
                await self._respond_ok(cseq)
                return False
            else:
                await self._respond_ok(cseq)

        return False

    async def _handle_describe(self, cseq):
        body = self._sdp.encode()
        headers = {"Content-Type": "application/sdp"}
        resp = build_rtsp_response(200, "OK", cseq, headers=headers, body=body)
        await self._write_downstream(resp)

    async def _handle_setup(self, url, cseq, headers):
        track_id = url.rstrip("/").split("/")[-1]
        track = self._tracks.get(track_id)
        transport = headers.get("transport", "")
        ds_rtp_ch, ds_rtcp_ch = parse_interleaved(transport)

        if track is None or ds_rtp_ch is None:
            resp = build_rtsp_response(404, "Not Found", cseq)
            await self._write_downstream(resp)
            return

        cam_rtp_ch = track.get("rtp_channel")
        cam_rtcp_ch = track.get("rtcp_channel")

        if cam_rtp_ch is not None:
            self._cam_to_ds_channel[cam_rtp_ch] = ds_rtp_ch
        if ds_rtcp_ch is not None and cam_rtcp_ch is not None:
            self._cam_to_ds_channel[cam_rtcp_ch] = ds_rtcp_ch

        if track.get("direction") == "sendonly":
            self._bc_ds_channel = ds_rtp_ch

        # Echo back exactly the transport the consumer asked for -- we're
        # not remapping its channels to anything, so there's nothing to
        # rewrite (unlike the transparent-relay ProxySession, which had to
        # splice in the camera's own channel numbers).
        resp_headers = {"Transport": transport, "Session": self._session_id}
        resp = build_rtsp_response(200, "OK", cseq, headers=resp_headers)
        await self._write_downstream(resp)

    async def _respond_ok(self, cseq):
        resp = build_rtsp_response(200, "OK", cseq, headers={"Session": self._session_id})
        await self._write_downstream(resp)

    # -- streaming ------------------------------------------------------------

    async def _stream_downstream(self):
        """Read the consumer's interleaved frames; route backchannel RTP upstream.

        Non-backchannel channels (e.g. RTCP receiver reports on a recvonly
        track) are not forwarded -- `CameraSession` only exposes the
        backchannel as a sink for consumer-originated media.
        """
        while not self._closed:
            item = await self.ds_reader.read_frame_or_message()

            if item[0] == "interleaved":
                _, channel, payload = item
                if channel == self._bc_ds_channel:
                    await self.camera.backchannel.emit(payload)
                continue

            _, first_line, headers, _body = item
            parts = first_line.split(" ", 2)
            if len(parts) < 2:
                continue
            method = parts[0]
            cseq = headers.get("cseq", "1")
            await self._respond_ok(cseq)
            if method == "TEARDOWN":
                break

    async def _on_media(self, channel, payload):
        """Registered with camera.add_consumer -- pushes camera media downstream."""
        ds_channel = self._cam_to_ds_channel.get(channel)
        if ds_channel is None:
            return
        frame = struct.pack(">cBH", b"$", ds_channel, len(payload)) + payload
        await self._write_downstream(frame)

    async def _write_downstream(self, data):
        async with self._ds_write_lock:
            self.ds_writer.write(data)
            await self.ds_writer.drain()
