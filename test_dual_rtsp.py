#!/usr/bin/env python3
"""Test: connect directly to Reolink doorbell via RTSP backchannel and play audio.

Usage:
  python3 test_dual_rtsp.py                    # just test connection
  python3 test_dual_rtsp.py chime-ding-dong.mp3  # connect and play audio file
"""

import hashlib
import os
import random
import socket
import struct
import subprocess
import sys
import time

HOST = "192.168.2.8"
PORT = 554
USER = "admin"
PASS = "rIda67wa289!"
STREAM = "h264Preview_01_sub"

BASE_URL = f"rtsp://{HOST}:{PORT}/{STREAM}"
AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")


def md5hex(s):
    return hashlib.md5(s.encode()).hexdigest()


def make_digest_auth(method, url, realm, nonce):
    ha1 = md5hex(f"{USER}:{realm}:{PASS}")
    ha2 = md5hex(f"{method}:{url}")
    response = md5hex(f"{ha1}:{nonce}:{ha2}")
    return (
        f'Digest username="{USER}", realm="{realm}", nonce="{nonce}", '
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


def send_request(sock, method, url, cseq, session=None, extra_headers=None):
    lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}"]
    if session:
        lines.append(f"Session: {session}")
    if extra_headers:
        for k, v in extra_headers.items():
            lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("")
    sock.sendall("\r\n".join(lines).encode())


def read_response(sock, buf=b""):
    while True:
        while len(buf) < 1:
            data = sock.recv(8192)
            if not data:
                raise ConnectionError("Connection closed")
            buf += data
        if buf[0:1] == b"$":
            while len(buf) < 4:
                buf += sock.recv(8192)
            length = struct.unpack(">H", buf[2:4])[0]
            while len(buf) < 4 + length:
                buf += sock.recv(8192)
            buf = buf[4 + length :]
        else:
            break

    while b"\r\n\r\n" not in buf:
        data = sock.recv(8192)
        if not data:
            raise ConnectionError("Connection closed")
        buf += data

    header_end = buf.index(b"\r\n\r\n")
    header_data = buf[:header_end].decode()
    buf = buf[header_end + 4 :]

    lines = header_data.split("\r\n")
    status_line = lines[0]
    headers = {}
    for line in lines[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            headers[k.lower()] = v

    body = b""
    if "content-length" in headers:
        cl = int(headers["content-length"])
        while len(buf) < cl:
            buf += sock.recv(8192)
        body = buf[:cl]
        buf = buf[cl:]

    status_code = int(status_line.split(" ")[1])
    return status_code, headers, body, buf


def convert_to_pcmu(filepath):
    """Convert audio file to raw PCMU (G.711 mu-law) 8kHz mono using ffmpeg."""
    # If already a .pcmu file, just read it directly
    if filepath.endswith(".pcmu"):
        print(f"Loading raw PCMU: {filepath}")
        with open(filepath, "rb") as f:
            data = f.read()
        print(f"Loaded: {len(data)} bytes ({len(data) / 8000:.1f}s)")
        return data

    print(f"Converting {filepath} to PCMU...")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", filepath, "-f", "mulaw", "-ar", "8000", "-ac", "1", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()}")
    duration = len(result.stdout) / 8000
    print(f"Converted: {len(result.stdout)} bytes ({duration:.1f}s)")
    return result.stdout


def send_audio(sock, pcmu_data, bc_channel, ptime_ms=20):
    """Send PCMU audio as RTP packets over the RTSP interleaved channel."""
    ssrc = random.randint(0, 0xFFFFFFFF)
    seq = 0
    timestamp = 0
    samples_per_packet = 8000 * ptime_ms // 1000  # 160 for 20ms
    offset = 0
    start = time.monotonic()
    packet_num = 0

    print(
        f"Sending audio: {len(pcmu_data)} bytes, {len(pcmu_data) / 8000:.1f}s, "
        f"channel={bc_channel}, {samples_per_packet} samples/packet..."
    )

    # Pre-build all RTP frames before sending for consistent timing
    frames = []
    while offset < len(pcmu_data):
        chunk = pcmu_data[offset : offset + samples_per_packet]
        if len(chunk) < samples_per_packet:
            chunk += b"\xff" * (samples_per_packet - len(chunk))

        seq = (seq + 1) & 0xFFFF
        rtp_header = struct.pack(
            ">BBHII",
            0x80,  # V=2
            0 | 0,  # No marker, PT=0 (PCMU)
            seq,
            timestamp,
            ssrc,
        )
        timestamp = (timestamp + len(chunk)) & 0xFFFFFFFF

        rtp_packet = rtp_header + chunk
        interleaved = struct.pack(">cBH", b"$", bc_channel, len(rtp_packet))
        frames.append(interleaved + rtp_packet)

        offset += samples_per_packet

    print(f"Pre-built {len(frames)} frames")

    # Concatenate all frames into one big buffer and send at once
    all_data = b"".join(frames)
    print(f"Sending {len(all_data)} bytes ({len(frames)} packets) in one burst...")
    sock.sendall(all_data)

    print(f"Done sending. {len(frames)} packets")


def main():
    audio_file = sys.argv[1] if len(sys.argv) > 1 else None

    # Convert audio first (before connecting) so we know it works
    pcmu_data = None
    if audio_file:
        filepath = audio_file
        if not os.path.isabs(filepath) and not os.path.exists(filepath):
            filepath = os.path.join(AUDIO_DIR, audio_file)
        if not os.path.exists(filepath):
            print(f"File not found: {filepath}")
            sys.exit(1)
        pcmu_data = convert_to_pcmu(filepath)

    print(f"\nConnecting to {HOST}:{PORT}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    try:
        sock.connect((HOST, PORT))
    except Exception as e:
        print(f"FAILED to connect: {e}")
        sys.exit(1)

    cseq = 1
    buf = b""
    realm = ""
    nonce = ""

    # OPTIONS
    send_request(sock, "OPTIONS", BASE_URL, cseq)
    status, headers, body, buf = read_response(sock, buf)
    print(f"OPTIONS: {status}")

    # DESCRIBE (expect 401, retry with auth)
    cseq += 1
    send_request(
        sock,
        "DESCRIBE",
        BASE_URL,
        cseq,
        extra_headers={
            "Accept": "application/sdp",
            "Require": "www.onvif.org/ver20/backchannel",
        },
    )
    status, headers, body, buf = read_response(sock, buf)

    if status == 401:
        www_auth = headers.get("www-authenticate", "")
        realm, nonce = parse_auth_challenge(www_auth)
        cseq += 1
        auth_header = make_digest_auth("DESCRIBE", BASE_URL, realm, nonce)
        send_request(
            sock,
            "DESCRIBE",
            BASE_URL,
            cseq,
            extra_headers={
                "Accept": "application/sdp",
                "Require": "www.onvif.org/ver20/backchannel",
                "Authorization": auth_header,
            },
        )
        status, headers, body, buf = read_response(sock, buf)

    if status != 200:
        print(f"DESCRIBE failed: {status}")
        sock.close()
        sys.exit(1)

    sdp = body.decode()
    print(f"\nSDP:\n{sdp}")

    # Parse tracks
    tracks = []
    current = None
    for line in sdp.strip().split("\n"):
        line = line.strip().rstrip("\r")
        if line.startswith("m="):
            if current:
                tracks.append(current)
            parts = line.split()
            current = {"kind": parts[0][2:], "direction": "recvonly", "m_line": line}
        elif current:
            if line.startswith("a=control:"):
                current["control"] = line.split(":", 1)[1]
            elif line == "a=sendonly":
                current["direction"] = "sendonly"
            elif line == "a=recvonly":
                current["direction"] = "recvonly"
            elif line.startswith("a=rtpmap:"):
                current["rtpmap"] = line.split(":", 1)[1]
    if current:
        tracks.append(current)

    print(f"Found {len(tracks)} tracks:")
    for t in tracks:
        print(
            f"  {t['kind']} {t['direction']} control={t.get('control', '?')} rtpmap={t.get('rtpmap', '?')}"
        )

    # SETUP only the backchannel (sendonly) track — skip video/audio receive
    # to avoid TCP backpressure from unconsumed incoming data
    session = None
    bc_channel = None
    bc_track = None
    for t in tracks:
        if t["direction"] == "sendonly":
            bc_track = t
            break

    if not bc_track:
        print("No backchannel track found in SDP!")
        sock.close()
        sys.exit(1)

    cseq += 1
    control = bc_track.get("control", "trackID=2")
    track_url = f"{BASE_URL}/{control}"
    transport = "RTP/AVP/TCP;unicast;interleaved=0-1"
    auth_header = make_digest_auth("SETUP", track_url, realm, nonce)
    print(f"\nSETUP {control} only (backchannel)...")
    send_request(
        sock,
        "SETUP",
        track_url,
        cseq,
        session=session,
        extra_headers={"Transport": transport, "Authorization": auth_header},
    )
    status, headers, body, buf = read_response(sock, buf)
    if status != 200:
        print(f"SETUP failed for {control}: {status}")
        sock.close()
        sys.exit(1)

    if "session" in headers:
        session = headers["session"].split(";")[0]

    resp_transport = headers.get("transport", "")
    if "interleaved=" in resp_transport:
        parts = resp_transport.split("interleaved=")[1].split(";")[0]
        bc_channel = int(parts.split("-")[0])
    else:
        bc_channel = 0
    print(f"Backchannel channel: {bc_channel} (codec: {bc_track.get('rtpmap', '?')})")

    # PLAY
    cseq += 1
    auth_header = make_digest_auth("PLAY", BASE_URL, realm, nonce)
    send_request(
        sock,
        "PLAY",
        BASE_URL,
        cseq,
        session=session,
        extra_headers={"Authorization": auth_header},
    )
    status, headers, body, buf = read_response(sock, buf)
    print(f"PLAY: {status}")

    if status != 200:
        print(f"PLAY failed: {status}")
        sock.close()
        sys.exit(1)

    if bc_channel is None:
        print("No backchannel track found!")
        sock.close()
        sys.exit(1)

    print(f"\nConnected! Backchannel on channel {bc_channel}")

    if pcmu_data:
        print(f"\nPlaying audio...")
        send_audio(sock, pcmu_data, bc_channel)
    else:
        print("No audio file specified. Use: python3 test_dual_rtsp.py <file.mp3>")

    # Teardown
    sock.settimeout(10)
    cseq += 1
    try:
        auth_header = make_digest_auth("TEARDOWN", BASE_URL, realm, nonce)
        send_request(
            sock,
            "TEARDOWN",
            BASE_URL,
            cseq,
            session=session,
            extra_headers={"Authorization": auth_header},
        )
    except Exception:
        pass
    sock.close()
    print("Done.")


if __name__ == "__main__":
    main()
