"""RTSP proxy that sits between go2rtc and a Reolink doorbell camera.

Maintains a single persistent `CameraSession` with a keep-warm lifecycle
(stays connected for KEEP_WARM_SECONDS after the last consumer leaves, so a
quick go2rtc reconnect doesn't churn the camera's one backchannel slot), and
fans that camera out to any number of `ConsumerSession`s.

Architecture:
  go2rtc <--RTSP--> proxy (localhost) <--RTSP--> doorbell (camera)
                       ^
                       | inject_chime()
                    cast-proxy server
"""
import asyncio
import contextlib
import os
import subprocess

from camera_session import CameraSession
from consumer_session import ConsumerSession


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
    """RTSP proxy between go2rtc (downstream) and a Reolink doorbell (upstream).

    Owns a single, keep-warm `CameraSession` and fans it out to any number
    of concurrently-connected `ConsumerSession`s (go2rtc, chime injection).
    The camera connection is established on demand (first consumer, or the
    first chime while nothing is connected) and torn down only after
    KEEP_WARM_SECONDS of having no consumers -- see `_ensure_camera` and
    `_schedule_teardown`.
    """

    KEEP_WARM_SECONDS = 30

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

        self._camera = None
        self._consumers = set()
        self._teardown_task = None
        # Serializes camera-lifecycle transitions (start in _ensure_camera,
        # stop in _later) so a new consumer can never observe -- or race
        # against -- a half-torn-down camera. See _ensure_camera/_later.
        self._lifecycle_lock = asyncio.Lock()

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, "0.0.0.0", self.listen_port
        )
        print(f"RTSP proxy listening on port {self.listen_port}")
        print(f"  Camera: {self.camera_host}:{self.camera_port}/{self.camera_stream}")
        print(f"  go2rtc should connect to: rtsp://localhost:{self.listen_port}/{self.camera_stream}")

    async def stop(self):
        if self._teardown_task:
            self._teardown_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._teardown_task
            self._teardown_task = None
        for consumer in list(self._consumers):
            await consumer.close()
        self._consumers.clear()
        if self._camera:
            await self._camera.stop()
            self._camera = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def inject_chime(self, filename):
        """Play an audio file through the backchannel (legacy, looks in AUDIO_DIR)."""
        filepath = os.path.join(AUDIO_DIR, filename)
        return await self.inject_chime_path(filepath)

    async def inject_chime_path(self, filepath):
        """Play a .pcmu audio file through the backchannel."""
        try:
            camera = await self._ensure_camera()
        except Exception as e:
            return f"Camera unavailable: {e}"

        if camera.backchannel is None or camera.backchannel._channel is None:
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
        await camera.backchannel.play_chime(pcmu_data)
        return "ok"

    async def _ensure_camera(self):
        """Return the live camera session, (re)connecting it if needed.

        Runs under `_lifecycle_lock`, and cancels any pending keep-warm
        teardown only *after* acquiring it -- not before. That ordering
        matters: if `_later()` (see `_schedule_teardown`) is still asleep or
        waiting on this same lock, cancelling it here pre-empts it cleanly
        before it ever touches the camera. But if `_later()` has already
        acquired the lock and is mid-`CameraSession.stop()`, this call
        blocks on the lock instead of racing in and cancelling that task --
        so a teardown in flight always runs to completion (leaving
        `self._camera` cleanly `None`) before we decide whether to build a
        fresh one. Without this, cancelling `_later()` while it's inside
        `stop()` would swallow the `CancelledError`, leave `self._camera`
        pointing at a half-torn-down session, and hand that to the new
        consumer.
        """
        async with self._lifecycle_lock:
            if self._teardown_task:
                self._teardown_task.cancel()
                self._teardown_task = None
            if self._camera is None or not self._camera.running:
                self._camera = CameraSession(
                    self.camera_host, self.camera_port,
                    self.camera_user, self.camera_pass, self.camera_stream,
                )
                await self._camera.start()
            return self._camera

    def _schedule_teardown(self):
        """Start (or restart) the keep-warm countdown after the last consumer leaves."""
        async def _later():
            try:
                await asyncio.sleep(self.KEEP_WARM_SECONDS)
                async with self._lifecycle_lock:
                    if not self._consumers and self._camera:
                        cam = self._camera
                        self._camera = None
                        await cam.stop()
            except asyncio.CancelledError:
                pass

        if self._teardown_task:
            self._teardown_task.cancel()
        self._teardown_task = asyncio.create_task(_later())

    async def _handle_client(self, reader, writer):
        addr = writer.get_extra_info("peername")
        print(f"RTSP proxy: new client from {addr}")

        try:
            camera = await self._ensure_camera()
        except Exception as e:
            print(f"RTSP proxy: camera unavailable, dropping client {addr}: {e}")
            with contextlib.suppress(Exception):
                writer.close()
                wait_closed = getattr(writer, "wait_closed", None)
                if wait_closed:
                    await wait_closed()
            return

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
                self._schedule_teardown()  # keep-warm, NOT immediate teardown
            print(f"RTSP proxy: client {addr} disconnected")

