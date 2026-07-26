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
