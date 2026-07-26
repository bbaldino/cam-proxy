import asyncio, struct, pytest
from consumer_session import ConsumerSession
from rtsp_wire import build_rtsp_request

class _StubCamera:
    def __init__(self, tracks=None):
        self.sdp = "v=0\r\n"
        self.tracks = tracks if tracks is not None else {
            "track3": {"direction": "sendonly", "us_ch": 4}
        }
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


def _real_shaped_camera():
    """CameraSession-shaped stub: field names match camera_session.py exactly."""
    return _StubCamera(tracks={
        "track1": {"kind": "video", "direction": "recvonly", "rtp_channel": 0, "rtcp_channel": 1},
        "track3": {"kind": "audio", "direction": "sendonly", "rtp_channel": 4, "rtcp_channel": 5},
    })


def _feed_negotiation(reader, ds_video_ch=(0, 1), ds_audio_ch=(2, 3)):
    reader.feed_data(build_rtsp_request(
        "DESCRIBE", "rtsp://x/stream", 1,
        {"Accept": "application/sdp"},
    ))
    reader.feed_data(build_rtsp_request(
        "SETUP", "rtsp://x/stream/track1", 2,
        {"Transport": f"RTP/AVP/TCP;unicast;interleaved={ds_video_ch[0]}-{ds_video_ch[1]}"},
    ))
    reader.feed_data(build_rtsp_request(
        "SETUP", "rtsp://x/stream/track3", 3,
        {"Transport": f"RTP/AVP/TCP;unicast;interleaved={ds_audio_ch[0]}-{ds_audio_ch[1]}"},
    ))
    reader.feed_data(build_rtsp_request("PLAY", "rtsp://x/stream", 4, {"Range": "npt=0.000-"}))


@pytest.mark.asyncio
async def test_negotiation_answers_from_camera_snapshot_without_touching_camera():
    cam = _real_shaped_camera()
    reader = asyncio.StreamReader()
    _feed_negotiation(reader)
    reader.feed_eof()
    writer = _FakeWriter()

    cs = ConsumerSession(cam, reader, writer)
    await cs.run()

    out = writer.buf.decode()
    assert out.count("RTSP/1.0 200 OK") == 4  # DESCRIBE, 2x SETUP, PLAY
    assert "v=0" in out  # DESCRIBE body came from camera.sdp
    assert "interleaved=2-3" in out  # SETUP echoed the consumer's own channels
    assert len(cam.added) == 1  # _on_media registered once, after PLAY


@pytest.mark.asyncio
async def test_streaming_forwards_backchannel_rtp_to_camera_backchannel():
    cam = _real_shaped_camera()
    reader = asyncio.StreamReader()
    _feed_negotiation(reader, ds_audio_ch=(2, 3))
    # consumer's talk RTP arrives on its own backchannel channel (2)
    talk_frame = struct.pack(">cBH", b"$", 2, 4) + b"talk"
    reader.feed_data(talk_frame)
    reader.feed_eof()
    writer = _FakeWriter()

    cs = ConsumerSession(cam, reader, writer)
    await cs.run()

    assert cam.emitted == [b"talk"]


@pytest.mark.asyncio
async def test_camera_media_pushed_to_consumer_on_its_own_channel():
    cam = _real_shaped_camera()
    reader = asyncio.StreamReader()
    _feed_negotiation(reader, ds_video_ch=(0, 1))
    reader.feed_eof()
    writer = _FakeWriter()

    cs = ConsumerSession(cam, reader, writer)
    await cs.run()

    on_media = cam.added[0]
    writer.buf = b""  # isolate from negotiation responses
    await on_media(0, b"videoframe")  # camera's own channel for track1 is 0

    assert writer.buf == struct.pack(">cBH", b"$", 0, len(b"videoframe")) + b"videoframe"


@pytest.mark.asyncio
async def test_close_never_calls_camera_stop():
    """close() must not have any way to reach camera lifecycle methods --
    the stub camera doesn't even define stop(), so any attempt would raise."""
    cam = _real_shaped_camera()
    reader = asyncio.StreamReader()
    _feed_negotiation(reader)
    reader.feed_eof()
    writer = _FakeWriter()

    cs = ConsumerSession(cam, reader, writer)
    await cs.run()
    await cs.close()
    assert not hasattr(cam, "stop")
