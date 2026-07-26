import asyncio, random, struct

class Backchannel:
    """Single owner of audio going UP to the camera.

    Sources (consumer talk, chime) feed in; emit() is the seam where they
    combine. Option 1 policy: forward talk, chime preempts. Becomes a real
    mixer (BackchannelMixer) in option 2.
    """
    def __init__(self, send_frame):
        self._send_frame = send_frame        # async (channel, payload) -> None
        self._channel = None
        self._chime_playing = False
        self._chime_done = asyncio.Event(); self._chime_done.set()
        self._lock = asyncio.Lock()
        self._ssrc = random.randint(0, 0xFFFFFFFF)
        self._seq = 0
        self._ts = 0

    def set_channel(self, upstream_channel):
        self._channel = upstream_channel

    @property
    def chime_playing(self):
        return self._chime_playing

    async def wait_chime_done(self):
        await self._chime_done.wait()

    async def emit(self, payload):
        if self._channel is None or self._chime_playing:
            return
        await self._send_frame(self._channel, payload)

    async def play_chime(self, pcmu_data):
        async with self._lock:
            if self._channel is None:
                return
            self._chime_playing = True
            self._chime_done.clear()
            try:
                start = asyncio.get_event_loop().time()
                n = 0
                for offset in range(0, len(pcmu_data), 160):
                    chunk = pcmu_data[offset:offset + 160]
                    if len(chunk) < 160:
                        chunk += b"\xff" * (160 - len(chunk))
                    self._seq = (self._seq + 1) & 0xFFFF
                    header = struct.pack(">BBHII", 0x80, 0, self._seq, self._ts, self._ssrc)
                    self._ts = (self._ts + len(chunk)) & 0xFFFFFFFF
                    await self._send_frame(self._channel, header + chunk)
                    n += 1
                    target = start + n * 0.020
                    now = asyncio.get_event_loop().time()
                    if target > now:
                        await asyncio.sleep(target - now)
            finally:
                self._chime_playing = False
                self._chime_done.set()
