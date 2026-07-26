import asyncio
import pytest
from rtsp_wire import (
    md5hex, make_digest_auth, parse_auth_challenge,
    build_rtsp_request, AsyncRtspReader, parse_interleaved,
)

def test_digest_matches_known_vector():
    # ha1=md5(user:realm:pass), ha2=md5(method:uri), resp=md5(ha1:nonce:ha2)
    got = make_digest_auth("DESCRIBE", "rtsp://x/y", "u", "p", "BC Streaming Media", "abc")
    ha1 = md5hex("u:BC Streaming Media:p")
    ha2 = md5hex("DESCRIBE:rtsp://x/y")
    assert md5hex(f"{ha1}:abc:{ha2}") in got
    assert 'realm="BC Streaming Media"' in got

def test_parse_auth_challenge():
    realm, nonce = parse_auth_challenge('Digest realm="BC Streaming Media", nonce="XYZ"')
    assert (realm, nonce) == ("BC Streaming Media", "XYZ")

def test_parse_interleaved():
    assert parse_interleaved("RTP/AVP/TCP;interleaved=4-5") == (4, 5)
    assert parse_interleaved("RTP/AVP/TCP;interleaved=4") == (4, None)

def test_build_rtsp_request_with_body():
    # Test that body is properly included and Content-Length header is added
    got = build_rtsp_request("PLAY", "rtsp://x", 1, body=b"abc")
    assert b"Content-Length: 3" in got
    assert got.endswith(b"abc")

    # Test that without body there is no Content-Length header
    got_no_body = build_rtsp_request("PLAY", "rtsp://x", 1)
    assert b"Content-Length" not in got_no_body
    assert not got_no_body.endswith(b"abc")

class _FakeStream:
    def __init__(self, data): self._data = data
    async def read(self, n):
        if not self._data: return b""
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

@pytest.mark.asyncio
async def test_reader_parses_interleaved_then_rtsp():
    import struct
    payload = b"\x00\x01\x02"
    frame = struct.pack(">cBH", b"$", 4, len(payload)) + payload
    msg = b"OPTIONS rtsp://x RTSP/1.0\r\nCSeq: 1\r\n\r\n"
    r = AsyncRtspReader(_FakeStream(frame + msg))
    assert await r.read_frame_or_message() == ("interleaved", 4, payload)
    kind, first_line, headers, body = await r.read_frame_or_message()
    assert kind == "rtsp" and headers["cseq"] == "1"
