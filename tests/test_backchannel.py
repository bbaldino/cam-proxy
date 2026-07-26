import asyncio, pytest
from backchannel import Backchannel

@pytest.mark.asyncio
async def test_emit_forwards_when_idle():
    sent = []
    bc = Backchannel(send_frame=lambda ch, p: sent.append((ch, p)) or asyncio.sleep(0))
    bc.set_channel(4)
    await bc.emit(b"talk")
    assert sent == [(4, b"talk")]

@pytest.mark.asyncio
async def test_emit_dropped_before_channel_known():
    sent = []
    bc = Backchannel(send_frame=lambda ch, p: sent.append((ch, p)) or asyncio.sleep(0))
    await bc.emit(b"talk")  # no channel yet
    assert sent == []

@pytest.mark.asyncio
async def test_chime_preempts_talk():
    sent = []
    async def sink(ch, p): sent.append((ch, p))
    bc = Backchannel(send_frame=sink)
    bc.set_channel(4)
    # 320 samples = two 20ms packets
    task = asyncio.create_task(bc.play_chime(b"\x00" * 320))
    await asyncio.sleep(0)              # let chime start
    assert bc.chime_playing
    await bc.emit(b"talk")              # should be dropped during chime
    await task
    assert not bc.chime_playing
    assert (4, b"talk") not in sent     # talk was preempted
    assert len(sent) == 2               # two chime packets emitted
