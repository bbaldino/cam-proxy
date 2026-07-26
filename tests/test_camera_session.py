import asyncio

import pytest

from camera_session import CameraSession


def test_fanout_registry_delivers_to_all_consumers():
    cs = CameraSession("h", 554, "u", "p", "s")
    got_a, got_b = [], []

    async def a(ch, p):
        got_a.append((ch, p))

    async def b(ch, p):
        got_b.append((ch, p))

    cs.add_consumer(a)
    cs.add_consumer(b)
    asyncio.run(cs._deliver(0, b"vid"))
    assert got_a == [(0, b"vid")] and got_b == [(0, b"vid")]
    cs.remove_consumer(a)
    asyncio.run(cs._deliver(0, b"vid2"))
    assert got_a == [(0, b"vid")] and got_b[-1] == (0, b"vid2")


def test_parse_sdp_marks_backchannel_track():
    cs = CameraSession("h", 554, "u", "p", "s")
    sdp = (
        "v=0\r\n"
        "m=video 0 RTP/AVP 96\r\na=control:track1\r\n"
        "m=audio 0 RTP/AVP 97\r\na=control:track3\r\na=sendonly\r\n"
    )
    cs._parse_sdp_tracks(sdp)
    assert cs.tracks["track3"]["direction"] == "sendonly"


def test_start_awaits_first_negotiation_before_returning():
    """start() must not return until sdp/tracks are populated, so a
    ConsumerSession can rely on them immediately after `await start()`.
    The connect/negotiate step is faked; the pump is faked to block
    forever (as it would against a real live connection) so we can
    assert start() already returned by the time we get here.
    """
    cs = CameraSession("h", 554, "u", "p", "s")

    async def fake_negotiate():
        cs.sdp = "v=0\r\n"
        cs.tracks = {"track1": {"kind": "video", "direction": "recvonly"}}

    async def fake_pump():
        await asyncio.Event().wait()  # never resolves; only stop() cancels it

    cs._connect_and_negotiate = fake_negotiate
    cs._pump = fake_pump

    async def scenario():
        await cs.start()
        assert cs.sdp == "v=0\r\n"
        assert cs.tracks["track1"]["kind"] == "video"
        assert cs.running is True
        await cs.stop()

    asyncio.run(scenario())
