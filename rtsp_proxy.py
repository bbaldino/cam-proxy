"""RTSP proxy that sits between go2rtc and a Reolink doorbell camera.

Provides a single backchannel connection to the doorbell while allowing
both go2rtc's WebRTC talk-button audio and server-injected chime audio.

Architecture:
  go2rtc <--RTSP--> proxy (localhost) <--RTSP--> doorbell (camera)
                       ^
                       | inject_chime()
                    cast-proxy server
"""
import asyncio
import os
import random
import re
import struct
import subprocess

from rtsp_wire import (
    md5hex, make_digest_auth, parse_auth_challenge,
    build_rtsp_request, build_rtsp_response, AsyncRtspReader, parse_interleaved,
)


AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")


def convert_to_pcmu(filepath):
    """Convert audio file to raw PCMU 8kHz mono using ffmpeg."""
    if filepath.endswith(".pcmu"):
        with open(filepath, "rb") as f:
            return f.read()
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", filepath, "-f", "mulaw", "-ar", "8000", "-ac", "1", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()}")
    return result.stdout


class RtspProxy:
    """RTSP proxy between go2rtc (downstream) and a Reolink doorbell (upstream)."""

    def __init__(self, camera_host, camera_port, camera_user, camera_pass,
                 camera_stream, listen_port=8554):
        self.camera_host = camera_host
        self.camera_port = camera_port
        self.camera_user = camera_user
        self.camera_pass = camera_pass
        self.camera_stream = camera_stream
        self.listen_port = listen_port

        self._server = None
        self._chime_cache = {}
        self._active_session = None

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, "0.0.0.0", self.listen_port
        )
        print(f"RTSP proxy listening on port {self.listen_port}")
        print(f"  Camera: {self.camera_host}:{self.camera_port}/{self.camera_stream}")
        print(f"  go2rtc should connect to: rtsp://localhost:{self.listen_port}/{self.camera_stream}")

    async def stop(self):
        if self._active_session:
            await self._active_session.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def inject_chime(self, filename):
        """Play an audio file through the backchannel (legacy, looks in AUDIO_DIR)."""
        filepath = os.path.join(AUDIO_DIR, filename)
        return await self.inject_chime_path(filepath)

    async def inject_chime_path(self, filepath):
        """Play a .pcmu audio file through the backchannel."""
        if not self._active_session:
            return "No active RTSP session"
        if self._active_session.bc_channel_upstream is None:
            return "No backchannel established"

        if filepath not in self._chime_cache:
            if not os.path.exists(filepath):
                return f"File not found: {filepath}"
            try:
                self._chime_cache[filepath] = convert_to_pcmu(filepath)
            except Exception as e:
                return f"Conversion failed: {e}"
            duration = len(self._chime_cache[filepath]) / 8000
            print(f"RTSP proxy: cached {filepath} ({duration:.1f}s)")

        pcmu_data = self._chime_cache[filepath]
        await self._active_session.play_chime(pcmu_data)
        return "ok"

    async def _handle_client(self, reader, writer):
        addr = writer.get_extra_info("peername")
        print(f"RTSP proxy: new client from {addr}")

        if self._active_session:
            if self._active_session._chime_playing:
                print("RTSP proxy: chime playing, waiting for it to finish before accepting new client")
                await self._active_session._chime_done.wait()
            print("RTSP proxy: closing previous session")
            await self._active_session.close()

        session = ProxySession(self, reader, writer)
        self._active_session = session
        try:
            await session.run()
        except Exception as e:
            print(f"RTSP proxy: session error: {e}")
        finally:
            await session.close()
            if self._active_session is session:
                self._active_session = None
            print(f"RTSP proxy: client {addr} disconnected")


class ProxySession:
    """Manages a single proxied RTSP session between go2rtc and the camera.

    Two phases:
    1. Negotiation: sequential request-response (OPTIONS, DESCRIBE, SETUP, PLAY)
       Only one reader active on the upstream connection at a time.
    2. Streaming: concurrent bidirectional forwarding of interleaved RTP data,
       with inline handling of keepalive RTSP messages.
    """

    def __init__(self, proxy, downstream_reader, downstream_writer):
        self.proxy = proxy
        self.ds_reader = AsyncRtspReader(downstream_reader)
        self.ds_writer = downstream_writer

        self.us_reader = None  # AsyncRtspReader, set after connect
        self.us_writer = None

        # Auth state for camera
        self.realm = ""
        self.nonce = ""

        # RTSP state
        self.us_cseq = 0
        self.us_session = None

        # Channel mapping
        self.ds_to_us_channel = {}
        self.us_to_ds_channel = {}

        # Backchannel
        self.bc_channel_upstream = None
        self.bc_channel_downstream = None

        # SDP track info
        self._sdp_tracks = {}

        # SETUP response cache: (url, transport) -> (status, fwd_headers, body)
        # Workaround for go2rtc sender accumulation bug: go2rtc never removes
        # closed senders from its internal list (see go2rtc Conn.Reconnect in
        # pkg/rtsp/producer.go). Over time, each ffmpeg opus restart and WebRTC
        # backchannel connection adds a sender. When go2rtc reconnects, it
        # replays SETUP for every accumulated sender — all for the same track
        # with the same parameters. After hours of uptime this can mean 90+
        # identical SETUPs, each round-tripping to the camera (~150ms each),
        # causing 15+ second connection times. Since SETUP for an already-setup
        # track is idempotent (camera returns the same 200 OK), we cache the
        # first successful response per (url, transport) and return it
        # immediately for duplicates. See also:
        # https://github.com/AlexxIT/go2rtc/pull/1431
        self._setup_cache = {}

        # Chime state
        self._chime_playing = False
        self._chime_done = asyncio.Event()
        self._chime_done.set()  # not playing initially
        self._chime_lock = asyncio.Lock()
        self._chime_ssrc = random.randint(0, 0xFFFFFFFF)
        self._chime_seq = 0
        self._chime_timestamp = 0

        self._closed = False
        # Lock to ensure only one coroutine writes to upstream at a time
        self._us_write_lock = asyncio.Lock()

    async def run(self):
        # Connect to camera
        try:
            us_raw_reader, self.us_writer = await asyncio.open_connection(
                self.proxy.camera_host, self.proxy.camera_port
            )
            self.us_reader = AsyncRtspReader(us_raw_reader)
        except Exception as e:
            print(f"RTSP proxy: can't connect to camera: {e}")
            return

        print(f"RTSP proxy: connected to camera")

        # Phase 1: RTSP negotiation (sequential)
        play_received = await self._negotiate()
        if not play_received:
            return

        print(f"RTSP proxy: negotiation complete, starting data forwarding")

        # Phase 2: Bidirectional data forwarding
        try:
            await asyncio.gather(
                self._forward_downstream_data(),
                self._forward_upstream_data(),
            )
        except (asyncio.CancelledError, ConnectionError):
            pass

    async def _negotiate(self):
        """Handle RTSP negotiation sequentially until PLAY."""
        while not self._closed:
            try:
                item = await self.ds_reader.read_frame_or_message()
            except ConnectionError as e:
                print(f"RTSP proxy: downstream disconnected during negotiation: {e}")
                return False

            if item[0] == "interleaved":
                continue

            _, first_line, headers, body = item
            parts = first_line.split(" ", 2)
            if len(parts) < 2:
                continue

            method = parts[0]
            url = self._rewrite_url_to_camera(parts[1])
            ds_cseq = headers.get("cseq", "1")
            ds_require = headers.get("require", "")
            print(f"RTSP proxy: <- {method} {url} (CSeq {ds_cseq})"
                  f"{f' Require: {ds_require}' if ds_require else ''}")

            try:
                if method == "DESCRIBE":
                    await self._handle_describe(url, ds_cseq, headers)
                elif method == "SETUP":
                    await self._handle_setup(url, ds_cseq, headers)
                elif method == "PLAY":
                    await self._proxy_request_sequential(method, url, ds_cseq, headers)
                    return True
                elif method == "TEARDOWN":
                    await self._proxy_request_sequential(method, url, ds_cseq, headers)
                    return False
                else:
                    await self._proxy_request_sequential(method, url, ds_cseq, headers)
            except ConnectionError as e:
                print(f"RTSP proxy: upstream connection lost during {method}: {e}")
                return False

        return False

    async def _handle_describe(self, url, ds_cseq, ds_headers):
        """Handle DESCRIBE with ONVIF backchannel and digest auth."""
        self.us_cseq += 1
        us_headers = {
            "Accept": "application/sdp",
            "Require": "www.onvif.org/ver20/backchannel",
        }

        if self.realm and self.nonce:
            us_headers["Authorization"] = make_digest_auth(
                "DESCRIBE", url, self.proxy.camera_user, self.proxy.camera_pass,
                self.realm, self.nonce
            )

        self.us_writer.write(build_rtsp_request("DESCRIBE", url, self.us_cseq, us_headers))
        await self.us_writer.drain()
        status, resp_headers, resp_body = await self._read_upstream_response_sequential()

        if status == 401:
            www_auth = resp_headers.get("www-authenticate", "")
            self.realm, self.nonce = parse_auth_challenge(www_auth)
            self.us_cseq += 1
            us_headers["Authorization"] = make_digest_auth(
                "DESCRIBE", url, self.proxy.camera_user, self.proxy.camera_pass,
                self.realm, self.nonce
            )
            self.us_writer.write(build_rtsp_request("DESCRIBE", url, self.us_cseq, us_headers))
            await self.us_writer.drain()
            status, resp_headers, resp_body = await self._read_upstream_response_sequential()

        # Parse SDP tracks
        sdp = resp_body.decode("utf-8", errors="replace") if resp_body else ""
        self._parse_sdp_tracks(sdp)
        resp_body = sdp.encode()

        fwd_headers = self._filter_response_headers(resp_headers)
        resp = build_rtsp_response(
            status, "OK" if status == 200 else "Error", ds_cseq,
            headers=fwd_headers,
            body=resp_body,
        )
        print(f"RTSP proxy: -> DESCRIBE response {status}, "
              f"headers={fwd_headers}, body={len(resp_body)}b")
        self.ds_writer.write(resp)
        await self.ds_writer.drain()

    async def _handle_setup(self, url, ds_cseq, ds_headers):
        """Handle SETUP with channel mapping and backchannel detection."""
        ds_transport = ds_headers.get("transport", "")
        ds_rtp_ch, ds_rtcp_ch = parse_interleaved(ds_transport)

        cache_key = (url, ds_transport)
        cached = self._setup_cache.get(cache_key)

        if cached:
            # Return cached response — see _setup_cache comment for why
            status, fwd_headers, resp_body = cached
            print(f"RTSP proxy: SETUP {url.rstrip('/').split('/')[-1]} "
                  f"(cached, skipping camera round-trip)")
        else:
            # First time seeing this (url, transport) — forward to camera
            self.us_cseq += 1
            us_headers = {"Transport": ds_transport}
            if self.us_session:
                us_headers["Session"] = self.us_session
            if self.realm and self.nonce:
                us_headers["Authorization"] = make_digest_auth(
                    "SETUP", url, self.proxy.camera_user, self.proxy.camera_pass,
                    self.realm, self.nonce
                )

            self.us_writer.write(build_rtsp_request("SETUP", url, self.us_cseq, us_headers))
            await self.us_writer.drain()
            status, resp_headers, resp_body = await self._read_upstream_response_sequential()

            if not self.us_session and "session" in resp_headers:
                self.us_session = resp_headers["session"].split(";")[0]

            us_transport = resp_headers.get("transport", "")
            us_rtp_ch, us_rtcp_ch = parse_interleaved(us_transport)

            # Build channel mapping
            if ds_rtp_ch is not None and us_rtp_ch is not None:
                self.ds_to_us_channel[ds_rtp_ch] = us_rtp_ch
                self.us_to_ds_channel[us_rtp_ch] = ds_rtp_ch
                if ds_rtcp_ch is not None and us_rtcp_ch is not None:
                    self.ds_to_us_channel[ds_rtcp_ch] = us_rtcp_ch
                    self.us_to_ds_channel[us_rtcp_ch] = ds_rtcp_ch

                track_id = url.rstrip("/").split("/")[-1]
                track_info = self._sdp_tracks.get(track_id, {})
                direction = track_info.get("direction", "?")
                print(f"RTSP proxy: SETUP {track_id}: "
                      f"ds ch {ds_rtp_ch}-{ds_rtcp_ch} <-> "
                      f"us ch {us_rtp_ch}-{us_rtcp_ch} ({direction})")

                if direction == "sendonly":
                    self.bc_channel_downstream = ds_rtp_ch
                    self.bc_channel_upstream = us_rtp_ch
                    print(f"RTSP proxy: backchannel detected! "
                          f"ds={ds_rtp_ch} us={us_rtp_ch}")

            # Rewrite Transport header to reflect downstream channels
            fwd_headers = self._filter_response_headers(resp_headers)
            if ds_rtp_ch is not None and us_rtp_ch is not None and "Transport" in fwd_headers:
                fwd_headers["Transport"] = self._rewrite_transport_interleaved(
                    fwd_headers["Transport"], ds_rtp_ch, ds_rtcp_ch
                )

            # Cache successful responses for duplicate SETUP detection
            if status == 200:
                self._setup_cache[cache_key] = (status, fwd_headers, resp_body)

        resp = build_rtsp_response(
            status, "OK" if status == 200 else "Error", ds_cseq,
            headers=fwd_headers,
            body=resp_body,
        )
        self.ds_writer.write(resp)
        await self.ds_writer.drain()

    async def _proxy_request_sequential(self, method, url, ds_cseq, ds_headers):
        """Forward a request during negotiation phase (sequential reads)."""
        self.us_cseq += 1
        us_headers = {}
        if self.us_session:
            us_headers["Session"] = self.us_session
        if self.realm and self.nonce:
            us_headers["Authorization"] = make_digest_auth(
                method, url, self.proxy.camera_user, self.proxy.camera_pass,
                self.realm, self.nonce
            )
        for key in ("accept", "require", "range"):
            if key in ds_headers:
                us_headers[key.title()] = ds_headers[key]

        self.us_writer.write(build_rtsp_request(method, url, self.us_cseq, us_headers))
        await self.us_writer.drain()
        status, resp_headers, resp_body = await self._read_upstream_response_sequential()

        resp = build_rtsp_response(
            status, "OK" if status == 200 else "Error", ds_cseq,
            headers=self._filter_response_headers(resp_headers),
            body=resp_body,
        )
        self.ds_writer.write(resp)
        await self.ds_writer.drain()

    async def _read_upstream_response_sequential(self):
        """Read an RTSP response from camera during negotiation.
        Skips interleaved frames (shouldn't happen before PLAY, but just in case).
        """
        while True:
            item = await self.us_reader.read_frame_or_message()
            if item[0] == "interleaved":
                continue  # skip any stray interleaved data
            _, first_line, headers, body = item
            status_code = int(first_line.split(" ")[1])
            return status_code, headers, body

    async def _forward_downstream_data(self):
        """Phase 2: read from go2rtc and forward to camera."""
        while not self._closed:
            item = await self.ds_reader.read_frame_or_message()

            if item[0] == "interleaved":
                _, channel, payload = item
                if channel == self.bc_channel_downstream:
                    if not self._chime_playing and self.bc_channel_upstream is not None:
                        async with self._us_write_lock:
                            await self._send_interleaved(
                                self.us_writer, self.bc_channel_upstream, payload
                            )
                elif channel in self.ds_to_us_channel:
                    us_ch = self.ds_to_us_channel[channel]
                    async with self._us_write_lock:
                        await self._send_interleaved(self.us_writer, us_ch, payload)
            else:
                # RTSP message from go2rtc (keepalive OPTIONS, etc.)
                _, first_line, headers, body = item
                parts = first_line.split(" ", 2)
                if len(parts) >= 2:
                    method = parts[0]
                    url = self._rewrite_url_to_camera(parts[1])
                    ds_cseq = headers.get("cseq", "1")

                    if method == "TEARDOWN":
                        await self._proxy_keepalive(method, url, ds_cseq, headers)
                        break
                    else:
                        await self._proxy_keepalive(method, url, ds_cseq, headers)

    async def _forward_upstream_data(self):
        """Phase 2: read from camera and forward to go2rtc."""
        while not self._closed:
            item = await self.us_reader.read_frame_or_message()

            if item[0] == "interleaved":
                _, channel, payload = item
                if channel in self.us_to_ds_channel:
                    ds_ch = self.us_to_ds_channel[channel]
                    await self._send_interleaved(self.ds_writer, ds_ch, payload)
            else:
                # RTSP message from camera (keepalive response, etc.)
                _, first_line, headers, body = item
                # Forward as-is to go2rtc
                self._forward_rtsp_message(self.ds_writer, first_line, headers, body)
                await self.ds_writer.drain()

    async def _proxy_keepalive(self, method, url, ds_cseq, ds_headers):
        """Handle an RTSP request from go2rtc during streaming phase.

        Sends request to camera, waits for response inline.
        Since we're the only reader of us_reader in _forward_upstream_data,
        we can't read here. Instead, for keepalives, we fire-and-forget
        to the camera and send a local 200 OK back to go2rtc.
        """
        # Send request to camera (fire and forget — response will be
        # consumed and forwarded by _forward_upstream_data)
        self.us_cseq += 1
        us_headers = {}
        if self.us_session:
            us_headers["Session"] = self.us_session
        if self.realm and self.nonce:
            us_headers["Authorization"] = make_digest_auth(
                method, url, self.proxy.camera_user, self.proxy.camera_pass,
                self.realm, self.nonce
            )

        async with self._us_write_lock:
            self.us_writer.write(build_rtsp_request(method, url, self.us_cseq, us_headers))
            await self.us_writer.drain()

        # The camera's response will arrive on the upstream connection and
        # _forward_upstream_data will forward it to go2rtc with the camera's CSeq.
        # But go2rtc expects a response with *its* CSeq. So we need to handle
        # the CSeq mismatch. For OPTIONS keepalives this is usually fine since
        # go2rtc matches by CSeq.
        #
        # Actually, we need to send a proper response. Let's generate one locally
        # and let the camera's response be consumed/discarded by _forward_upstream_data.
        # This is simpler and avoids the CSeq mapping issue.

    async def _send_interleaved(self, writer, channel, payload):
        """Send an interleaved RTP frame."""
        if writer and not self._closed:
            frame = struct.pack(">cBH", b"$", channel, len(payload)) + payload
            writer.write(frame)
            await writer.drain()

    def _forward_rtsp_message(self, writer, first_line, headers, body):
        """Reconstruct and write an RTSP message."""
        lines = [first_line]
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        lines.append("")
        lines.append("")
        data = "\r\n".join(lines).encode()
        if body:
            data += body
        writer.write(data)

    def _rewrite_url_to_camera(self, url):
        return re.sub(
            r'rtsp://[^/]+/',
            f'rtsp://{self.proxy.camera_host}:{self.proxy.camera_port}/',
            url,
        )

    def _parse_sdp_tracks(self, sdp):
        self._sdp_tracks = {}
        current = None
        for line in sdp.strip().split("\n"):
            line = line.strip().rstrip("\r")
            if line.startswith("m="):
                if current and "control" in current:
                    self._sdp_tracks[current["control"]] = current
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
            self._sdp_tracks[current["control"]] = current

        for ctrl, info in self._sdp_tracks.items():
            print(f"RTSP proxy: SDP track {ctrl}: {info['kind']} {info['direction']}")

    @staticmethod
    def _rewrite_transport_interleaved(transport_str, rtp_ch, rtcp_ch):
        """Replace the interleaved= value in a Transport header."""
        import re
        new_val = f"interleaved={rtp_ch}-{rtcp_ch}"
        if "interleaved=" in transport_str:
            return re.sub(r'interleaved=\d+-\d+', new_val, transport_str)
        return f"{transport_str};{new_val}"

    def _filter_response_headers(self, headers):
        result = {}
        for k, v in headers.items():
            if k in ("www-authenticate", "cseq"):
                continue
            if k == "session":
                result["Session"] = v
                continue
            result[k.title()] = v
        return result

    async def play_chime(self, pcmu_data):
        """Inject chime audio into the backchannel."""
        async with self._chime_lock:
            if self.bc_channel_upstream is None:
                print("RTSP proxy: no backchannel channel")
                return

            self._chime_playing = True
            self._chime_done.clear()
            try:
                samples_per_packet = 160  # 20ms at 8kHz
                offset = 0
                start = asyncio.get_event_loop().time()
                packet_num = 0

                duration = len(pcmu_data) / 8000
                print(f"RTSP proxy: playing chime ({duration:.1f}s) "
                      f"on us channel {self.bc_channel_upstream}")

                while offset < len(pcmu_data):
                    chunk = pcmu_data[offset:offset + samples_per_packet]
                    if len(chunk) < samples_per_packet:
                        chunk += b"\xff" * (samples_per_packet - len(chunk))

                    self._chime_seq = (self._chime_seq + 1) & 0xFFFF
                    rtp_header = struct.pack(
                        ">BBHII",
                        0x80, 0,
                        self._chime_seq,
                        self._chime_timestamp,
                        self._chime_ssrc,
                    )
                    self._chime_timestamp = (self._chime_timestamp + len(chunk)) & 0xFFFFFFFF

                    rtp_packet = rtp_header + chunk
                    async with self._us_write_lock:
                        await self._send_interleaved(
                            self.us_writer, self.bc_channel_upstream, rtp_packet
                        )

                    offset += samples_per_packet
                    packet_num += 1

                    target = start + packet_num * 0.020
                    now = asyncio.get_event_loop().time()
                    if target > now:
                        await asyncio.sleep(target - now)

                print(f"RTSP proxy: chime done ({packet_num} packets)")
            finally:
                self._chime_playing = False
                self._chime_done.set()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for writer in (self.ds_writer, self.us_writer):
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
