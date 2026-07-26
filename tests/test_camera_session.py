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


def test_deliver_survives_a_raising_consumer_and_still_delivers_to_others():
    """A consumer callback raising (e.g. its downstream writer died) must
    not propagate out of _deliver -- _run_loop treats any exception from
    the pump as a camera connection error and reconnects, which would
    churn the shared camera for every other consumer over one dead
    downstream. _deliver should log, drop the offending consumer, and
    keep going."""
    cs = CameraSession("h", 554, "u", "p", "s")
    got_b = []

    async def dies(ch, p):
        raise ConnectionResetError("downstream gone")

    async def b(ch, p):
        got_b.append((ch, p))

    cs.add_consumer(dies)
    cs.add_consumer(b)

    asyncio.run(cs._deliver(0, b"vid"))  # must not raise

    assert got_b == [(0, b"vid")]
    assert dies not in cs._consumers  # dropped after raising


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


def test_start_closes_socket_on_failed_first_negotiation():
    """A failed first negotiation must not leak the half-open socket — the
    doorbell only tolerates one backchannel connection cleanly, so a
    dangling connection here can break the next attempt.
    """
    cs = CameraSession("h", 554, "u", "p", "s")

    class FakeWriter:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    fake_writer = FakeWriter()

    async def fake_negotiate_then_fail():
        cs._writer = fake_writer
        cs._reader = object()  # stand-in; only its None-ness is checked
        raise ConnectionError("SETUP failed: 500")

    cs._connect_and_negotiate = fake_negotiate_then_fail

    async def scenario():
        with pytest.raises(ConnectionError):
            await cs.start()

    asyncio.run(scenario())

    assert fake_writer.closed is True
    assert cs._writer is None
    assert cs._reader is None
    assert cs.running is False
