import asyncio
import pytest

from rtsp_proxy import RtspProxy


@pytest.mark.asyncio
async def test_camera_persists_across_consumer_churn(monkeypatch):
    starts = {"n": 0}

    class FakeCamera:
        def __init__(self, *a):
            starts["n"] += 1
            self.running = True

        async def start(self):
            pass

        async def stop(self):
            self.running = False

        backchannel = None

    monkeypatch.setattr("rtsp_proxy.CameraSession", FakeCamera)
    p = RtspProxy("h", 554, "u", "p", "s")
    await p._ensure_camera()
    await p._ensure_camera()  # second consumer arrives
    assert starts["n"] == 1  # only ONE camera session created


@pytest.mark.asyncio
async def test_inject_chime_without_camera_reports_gracefully(monkeypatch):
    p = RtspProxy("h", 554, "u", "p", "s")
    # no camera warm, file missing -> returns a string, never raises AttributeError
    result = await p.inject_chime_path("/nonexistent.pcmu")
    assert isinstance(result, str)
