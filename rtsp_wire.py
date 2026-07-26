"""Pure RTSP wire protocol helpers for building and parsing messages."""
import hashlib
import re
import struct


def md5hex(s):
    return hashlib.md5(s.encode()).hexdigest()


def make_digest_auth(method, url, user, password, realm, nonce):
    ha1 = md5hex(f"{user}:{realm}:{password}")
    ha2 = md5hex(f"{method}:{url}")
    response = md5hex(f"{ha1}:{nonce}:{ha2}")
    return (
        f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
        f'uri="{url}", response="{response}"'
    )


def parse_auth_challenge(header_value):
    parts = {}
    rest = header_value.replace("Digest ", "").strip()
    for item in rest.split(","):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip()] = v.strip().strip('"')
    return parts.get("realm", ""), parts.get("nonce", "")


def build_rtsp_response(status_code, status_text, cseq, headers=None, body=b""):
    lines = [f"RTSP/1.0 {status_code} {status_text}"]
    lines.append(f"CSeq: {cseq}")
    if headers:
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    lines.append("")
    lines.append("")
    result = "\r\n".join(lines).encode()
    if body:
        result += body
    return result


def build_rtsp_request(method, url, cseq, headers=None, body=b""):
    lines = [f"{method} {url} RTSP/1.0"]
    lines.append(f"CSeq: {cseq}")
    if headers:
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("")
    return "\r\n".join(lines).encode()


def parse_interleaved(transport):
    m = re.search(r"interleaved=(\d+)(?:-(\d+))?", transport)
    if not m:
        return None, None
    rtp = int(m.group(1))
    rtcp = int(m.group(2)) if m.group(2) else None
    return rtp, rtcp


class AsyncRtspReader:
    """Buffered reader for RTSP interleaved streams.

    Handles the mixed binary ($ framed RTP) and text (RTSP messages) protocol.
    Only one coroutine should call methods on this reader at a time.
    """

    def __init__(self, reader):
        self._reader = reader
        self._buf = b""

    async def _fill(self, min_bytes=1):
        while len(self._buf) < min_bytes:
            data = await self._reader.read(65536)
            if not data:
                raise ConnectionError("Connection closed")
            self._buf += data

    async def read_frame_or_message(self):
        """Read the next item from the stream.

        Returns one of:
          ("interleaved", channel, payload)  — for $ framed RTP data
          ("rtsp", first_line, headers, body) — for an RTSP message
        """
        await self._fill(1)

        if self._buf[0:1] == b"$":
            # Interleaved RTP frame
            await self._fill(4)
            channel = self._buf[1]
            frame_len = struct.unpack(">H", self._buf[2:4])[0]
            total = 4 + frame_len
            await self._fill(total)
            payload = self._buf[4:total]
            self._buf = self._buf[total:]
            return ("interleaved", channel, payload)

        # RTSP text message — read until \r\n\r\n
        while b"\r\n\r\n" not in self._buf:
            await self._fill(len(self._buf) + 1)

        end = self._buf.index(b"\r\n\r\n")
        header_data = self._buf[:end].decode("utf-8", errors="replace")
        consumed = end + 4

        lines = header_data.split("\r\n")
        first_line = lines[0]
        headers = {}
        for line in lines[1:]:
            if ": " in line:
                k, v = line.split(": ", 1)
                headers[k.lower()] = v

        body = b""
        if "content-length" in headers:
            cl = int(headers["content-length"])
            await self._fill(consumed + cl)
            body = self._buf[consumed:consumed + cl]
            consumed += cl

        self._buf = self._buf[consumed:]
        return ("rtsp", first_line, headers, body)
