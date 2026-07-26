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
async def test_ensure_camera_never_hands_out_a_half_torn_down_camera(monkeypatch):
    """A consumer landing while the keep-warm teardown is mid-`stop()` must
    never observe a camera that's been interrupted partway through
    shutdown. `_ensure_camera` must either pre-empt the teardown before it
    touches the camera (if it hasn't taken the lock yet), or wait for the
    in-flight `stop()` to run to completion and then build a fresh camera
    -- never race in and cancel `stop()` mid-flight.
    """
    events = []
    stop_entered = asyncio.Event()
    stop_may_finish = asyncio.Event()

    class FakeCamera:
        def __init__(self, *a):
            self.running = True

        async def start(self):
            pass

        async def stop(self):
            events.append("stop-begin")
            stop_entered.set()
            await stop_may_finish.wait()
            self.running = False
            events.append("stop-end")

        backchannel = None

    monkeypatch.setattr("rtsp_proxy.CameraSession", FakeCamera)
    p = RtspProxy("h", 554, "u", "p", "s")
    p.KEEP_WARM_SECONDS = 0

    camera1 = await p._ensure_camera()

    # Last consumer left -> keep-warm timer starts (fires ~immediately since
    # KEEP_WARM_SECONDS is 0).
    p._schedule_teardown()

    # Let the teardown task run up through the point where it's blocked
    # inside camera1.stop().
    await stop_entered.wait()

    # A new consumer arrives right as teardown is mid-stop(). This must
    # block on the lifecycle lock rather than interrupting stop().
    ensure_task = asyncio.create_task(p._ensure_camera())
    await asyncio.sleep(0)
    assert not ensure_task.done(), "_ensure_camera must wait for the in-flight stop() to finish"

    # Let the in-flight stop() complete.
    stop_may_finish.set()
    camera2 = await ensure_task

    assert events == ["stop-begin", "stop-end"]
    assert camera2.running is True  # never a stopped/half-torn camera
    assert camera1.running is False  # the old one really did finish stopping
    assert camera2 is not camera1  # teardown won the race -> fresh camera built


@pytest.mark.asyncio
async def test_ensure_camera_cancels_teardown_still_sleeping(monkeypatch):
    """If the keep-warm countdown hasn't fired yet (still asleep), a new
    consumer arriving must simply cancel it and reuse the still-live
    camera -- no stop/restart churn."""
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

    camera1 = await p._ensure_camera()
    p._schedule_teardown()  # KEEP_WARM_SECONDS is the real 30s -- stays asleep

    camera2 = await p._ensure_camera()

    assert starts["n"] == 1
    assert camera2 is camera1
    assert camera2.running is True
    assert p._teardown_task is None


@pytest.mark.asyncio
async def test_inject_chime_reports_when_camera_unavailable():
    p = RtspProxy("h", 554, "u", "p", "s")
    # No camera warm; real CameraSession construction against unreachable
    # host "h" fails fast at connect -> caught and reported as a string,
    # never an unhandled exception.
    result = await p.inject_chime_path("/nonexistent.pcmu")
    assert isinstance(result, str)
    assert "unavailable" in result.lower()


@pytest.mark.asyncio
async def test_inject_chime_reports_when_backchannel_not_established(monkeypatch):
    """Exercises the backchannel._channel is None guard specifically --
    the camera connects fine, it just hasn't set up its sendonly track yet.
    """
    class FakeBackchannel:
        _channel = None

    class FakeCamera:
        def __init__(self, *a):
            self.running = True

        async def start(self):
            pass

        async def stop(self):
            self.running = False

        backchannel = FakeBackchannel()

    monkeypatch.setattr("rtsp_proxy.CameraSession", FakeCamera)
    p = RtspProxy("h", 554, "u", "p", "s")

    result = await p.inject_chime_path("/nonexistent.pcmu")
    assert result == "No backchannel established"


@pytest.mark.asyncio
async def test_handle_client_closes_writer_when_camera_unavailable(monkeypatch):
    """If _ensure_camera() fails (e.g. camera unreachable), _handle_client
    must close the downstream writer and return cleanly -- not leak the
    socket by letting the exception propagate out of the connection task."""

    class FailingCamera:
        def __init__(self, *a):
            pass

        async def start(self):
            raise ConnectionError("camera unreachable")

    monkeypatch.setattr("rtsp_proxy.CameraSession", FailingCamera)
    p = RtspProxy("h", 554, "u", "p", "s")

    calls = {"close": False, "wait_closed": False}

    class FakeWriter:
        def get_extra_info(self, name):
            return ("1.2.3.4", 5555)

        def close(self):
            calls["close"] = True

        async def wait_closed(self):
            calls["wait_closed"] = True

    class FakeReader:
        pass

    # Must not raise.
    await p._handle_client(FakeReader(), FakeWriter())

    assert calls["close"] is True
    assert calls["wait_closed"] is True
    assert len(p._consumers) == 0
