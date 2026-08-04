#!/usr/bin/env python3
"""HTTP + WebSocket proxy server for go2rtc with DashCast trigger."""

import asyncio
import json
import os
import re

import aiohttp
import pychromecast
from aiohttp import web
from gtts import gTTS
from pychromecast.controllers.dashcast import DashCastController

from theme_origins import (
    DEFAULT_THEME_ORIGINS,
    inject_theme_origins,
    insecure_origins,
    parse_theme_origins,
)

GO2RTC = os.environ.get("GO2RTC_HOST", "localhost:1984")
SERVE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8899"))

# RTSP proxy settings
CAMERA_HOST = os.environ.get("CAMERA_HOST", "")
CAMERA_PORT = int(os.environ.get("CAMERA_PORT", "554"))
CAMERA_USER = os.environ.get("CAMERA_USER", "")
CAMERA_PASS = os.environ.get("CAMERA_PASS", "")
CAMERA_STREAM = os.environ.get("CAMERA_STREAM", "")
RTSP_PROXY_PORT = int(os.environ.get("RTSP_PROXY_PORT", "8554"))

# Origins permitted to post theming CSS into the doorbell page.
THEME_ORIGINS = parse_theme_origins(
    os.environ.get("DOORBELL_THEME_ORIGINS", DEFAULT_THEME_ORIGINS)
)
for _origin in insecure_origins(THEME_ORIGINS):
    print(
        f"WARNING: theme origin {_origin} is not a secure origin. A parent frame "
        f"there cannot host WebRTC or microphone capture in the doorbell iframe, "
        f"because a document nested in an insecure ancestor is not a secure context."
    )


async def ws_proxy(request):
    """Proxy WebSocket connections to go2rtc."""
    src = request.query.get("src", "")
    target = f"ws://{GO2RTC}/api/ws?src={src}"
    print(f"WS proxy: {request.remote} -> {target}")

    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(target) as ws_server:

            async def forward_to_server():
                async for msg in ws_client:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        print(f"  -> server: {msg.data[:100]}")
                        await ws_server.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_server.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break

            async def forward_to_client():
                async for msg in ws_server:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        print(f"  <- client: {msg.data[:100]}")
                        await ws_client.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await ws_client.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break

            await asyncio.gather(
                forward_to_server(), forward_to_client(), return_exceptions=True
            )

    return ws_client


async def http_proxy(request):
    """Proxy HTTP requests to go2rtc."""
    import time

    t0 = time.monotonic()
    path = request.path[len("/api/go2rtc") :]
    target = f"http://{GO2RTC}{path}"
    if request.query_string:
        target += f"?{request.query_string}"

    body = await request.read()
    t1 = time.monotonic()
    session = request.app["client_session"]
    try:
        async with session.request(
            request.method,
            target,
            data=body if body else None,
            headers={"Content-Type": request.content_type}
            if request.content_type
            else {},
        ) as resp:
            data = await resp.read()
            t2 = time.monotonic()
            print(
                f"Proxy {request.method} {path} -> read body: {(t1 - t0) * 1000:.0f}ms, go2rtc: {(t2 - t1) * 1000:.0f}ms, total: {(t2 - t0) * 1000:.0f}ms"
            )
            return web.Response(
                body=data, status=resp.status, content_type=resp.content_type
            )
    except (aiohttp.ClientError, ConnectionError) as e:
        print(f"Proxy {request.method} {path} -> error: {e}")
        return web.Response(text=str(e), status=502)


class CastManager:
    """Manages persistent chromecast connections with doorbell dedup."""

    DOORBELL_URL_MARKER = "webrtc-doorbell"

    def __init__(self):
        self._casts = {}  # device_name -> cast object
        self._dashcast = {}  # device_name -> DashCastController
        self._browser = None
        self._showing_doorbell = set()  # device names currently showing doorbell
        self._lock = asyncio.Lock()

    def _get_or_discover(self, device):
        """Get cached cast or discover it. Must be called from executor."""
        if device in self._casts:
            cast = self._casts[device]
            if cast.socket_client and cast.socket_client.is_connected:
                return cast
            else:
                print(f"Cast: {device} connection stale, rediscovering")
                self._casts.pop(device, None)
                self._dashcast.pop(device, None)

        chromecasts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=[device]
        )
        if self._browser:
            self._browser.stop_discovery()
        self._browser = browser

        if not chromecasts:
            browser.stop_discovery()
            self._browser = None
            return None

        cast = chromecasts[0]
        cast.wait()
        self._casts[device] = cast

        d = DashCastController()
        cast.register_handler(d)
        self._dashcast[device] = d

        print(f"Cast: connected to {device}")
        return cast

    def cast_url(self, device, url):
        """Cast a URL to a device. Skips if already showing doorbell."""
        is_doorbell = self.DOORBELL_URL_MARKER in url

        if is_doorbell and device in self._showing_doorbell:
            return f"Already showing doorbell on {device}"

        cast = self._get_or_discover(device)
        if not cast:
            return f"Device '{device}' not found"

        d = self._dashcast[device]
        d.load_url(url, force=True)

        if is_doorbell:
            self._showing_doorbell.add(device)
        else:
            self._showing_doorbell.discard(device)

        return f"Cast sent: {url} -> {device}"


cast_manager = CastManager()


async def cast_trigger(request):
    """Trigger DashCast to load a URL on a Chromecast."""
    device = request.query.get("device", "Office display")
    stream = request.query.get("stream", "doorbell_sub")
    host = request.host.split(":")[0] if request.host else "localhost"
    url = request.query.get("url", f"http://{host}:{PORT}/cast.html?stream={stream}")

    async with cast_manager._lock:
        result = await asyncio.get_event_loop().run_in_executor(
            None, cast_manager.cast_url, device, url
        )
    print(result)
    return web.Response(text=result)


AUDIO_DIR = os.environ.get("AUDIO_DIR", os.path.join(SERVE_DIR, "audio"))
CHIMES_DIR = os.path.join(AUDIO_DIR, "chimes")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(CHIMES_DIR, exist_ok=True)
MESSAGES_META = os.path.join(AUDIO_DIR, "messages.json")
SLOTS_FILE = os.path.join(AUDIO_DIR, "slots.json")
CHIME_CONFIG_FILE = os.path.join(AUDIO_DIR, "chime.json")


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


def convert_to_pcmu(mp3_path, pcmu_path):
    """Convert an MP3 file to raw PCMU (G.711 mu-law) 8kHz mono."""
    import subprocess

    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            mp3_path,
            "-f",
            "mulaw",
            "-ar",
            "8000",
            "-ac",
            "1",
            pcmu_path,
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"ffmpeg PCMU conversion failed: {result.stderr.decode()}")
        return False
    return True


def ensure_pcmu(msg_id):
    """Ensure a .pcmu file exists for the given message ID."""
    mp3_path = os.path.join(AUDIO_DIR, f"{msg_id}.mp3")
    pcmu_path = os.path.join(AUDIO_DIR, f"{msg_id}.pcmu")
    if not os.path.exists(pcmu_path) and os.path.exists(mp3_path):
        convert_to_pcmu(mp3_path, pcmu_path)


def get_message_info(msg_id, meta):
    return {
        "id": msg_id,
        "text": meta.get(msg_id, {}).get("text", msg_id),
        "file": f"audio/{msg_id}.mp3",
        "pcmu": f"audio/{msg_id}.pcmu",
    }


async def list_messages(request):
    """List all available pre-recorded messages."""
    meta = load_json(MESSAGES_META, {})
    messages = []
    for f in sorted(os.listdir(AUDIO_DIR)):
        if f.endswith(".mp3"):
            msg_id = f[:-4]
            messages.append(get_message_info(msg_id, meta))
    return web.json_response(messages)


async def create_message(request):
    """Generate a TTS message and save it."""
    data = await request.json()
    text = data.get("text", "").strip()
    name = data.get("name", "").strip() or slugify(text)
    if not text:
        return web.json_response({"error": "text is required"}, status=400)

    def generate():
        tts = gTTS(text=text, lang=data.get("lang", "en"))
        mp3_path = os.path.join(AUDIO_DIR, f"{name}.mp3")
        tts.save(mp3_path)
        convert_to_pcmu(mp3_path, os.path.join(AUDIO_DIR, f"{name}.pcmu"))
        return name

    name = await asyncio.get_event_loop().run_in_executor(None, generate)
    meta = load_json(MESSAGES_META, {})
    meta[name] = {"text": text}
    save_json(MESSAGES_META, meta)
    print(f"Generated TTS: {name} -> '{text}'")
    return web.json_response(get_message_info(name, meta))


async def delete_message(request):
    """Delete a pre-recorded message."""
    name = request.match_info["name"]
    path = os.path.join(AUDIO_DIR, f"{name}.mp3")
    if not os.path.exists(path):
        return web.json_response({"error": "not found"}, status=404)
    os.remove(path)
    pcmu_path = os.path.join(AUDIO_DIR, f"{name}.pcmu")
    if os.path.exists(pcmu_path):
        os.remove(pcmu_path)
    meta = load_json(MESSAGES_META, {})
    meta.pop(name, None)
    save_json(MESSAGES_META, meta)
    # Remove from slots if present
    slots = load_json(SLOTS_FILE, [])
    if name in slots:
        slots.remove(name)
        save_json(SLOTS_FILE, slots)
    return web.json_response({"deleted": name})


async def get_slots(request):
    """Get the ordered list of messages assigned to doorbell slots."""
    meta = load_json(MESSAGES_META, {})
    slots = load_json(SLOTS_FILE, [])
    # Filter out any stale slot IDs
    result = []
    for msg_id in slots:
        if os.path.exists(os.path.join(AUDIO_DIR, f"{msg_id}.mp3")):
            result.append(get_message_info(msg_id, meta))
    return web.json_response(result)


async def set_slots(request):
    """Set the ordered list of message IDs for doorbell slots."""
    data = await request.json()
    slot_ids = data if isinstance(data, list) else []
    # Validate all IDs exist
    valid = [s for s in slot_ids if os.path.exists(os.path.join(AUDIO_DIR, f"{s}.mp3"))]
    save_json(SLOTS_FILE, valid)
    meta = load_json(MESSAGES_META, {})
    return web.json_response([get_message_info(s, meta) for s in valid])


CORS_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
]


@web.middleware
async def common_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    # CORS
    origin = request.headers.get("Origin", "")
    if origin in CORS_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # No-cache for HTML/JS so updates are picked up immediately
    if request.path.endswith((".html", ".js")):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def ensure_chime_pcmu(chime_id):
    """Ensure a .pcmu file exists for a chime (converting from .mp3 if needed)."""
    pcmu_path = os.path.join(CHIMES_DIR, f"{chime_id}.pcmu")
    if os.path.exists(pcmu_path):
        return True
    mp3_path = os.path.join(CHIMES_DIR, f"{chime_id}.mp3")
    if os.path.exists(mp3_path):
        return convert_to_pcmu(mp3_path, pcmu_path)
    return False


def list_chime_files():
    """Discover available chime sounds from the chimes directory."""
    return sorted(
        os.path.splitext(f)[0] for f in os.listdir(CHIMES_DIR) if f.endswith(".mp3")
    )


async def list_chimes(request):
    """List all available doorbell chime sounds."""
    chimes = []
    for name in list_chime_files():
        has_mp3 = os.path.exists(os.path.join(CHIMES_DIR, f"{name}.mp3"))
        has_pcmu = os.path.exists(os.path.join(CHIMES_DIR, f"{name}.pcmu"))
        chimes.append(
            {
                "id": name,
                "name": name.replace("-", " ").replace("_", " ").title(),
                "has_mp3": has_mp3,
                "has_pcmu": has_pcmu,
            }
        )
    return web.json_response(chimes)


async def get_chime_config(request):
    """Get the current doorbell chime configuration."""
    config = load_json(CHIME_CONFIG_FILE, {"file": ""})
    return web.json_response(config)


async def set_chime_config(request):
    """Set the doorbell chime file."""
    data = await request.json()
    config = {"file": data.get("file", "")}
    save_json(CHIME_CONFIG_FILE, config)
    print(f"Chime config updated: {config['file']}")
    return web.json_response(config)


def resolve_audio_to_pcmu(audio_id):
    """Resolve an audio ID to a .pcmu file path, searching chimes then messages.

    Returns the full path to the .pcmu file, or None if not found.
    Auto-converts from .mp3 if needed.
    """
    # Check chimes directory first
    pcmu_path = os.path.join(CHIMES_DIR, f"{audio_id}.pcmu")
    mp3_path = os.path.join(CHIMES_DIR, f"{audio_id}.mp3")
    if os.path.exists(pcmu_path):
        return pcmu_path
    if os.path.exists(mp3_path):
        if convert_to_pcmu(mp3_path, pcmu_path):
            return pcmu_path

    # Check messages directory
    pcmu_path = os.path.join(AUDIO_DIR, f"{audio_id}.pcmu")
    mp3_path = os.path.join(AUDIO_DIR, f"{audio_id}.mp3")
    if os.path.exists(pcmu_path):
        return pcmu_path
    if os.path.exists(mp3_path):
        if convert_to_pcmu(mp3_path, pcmu_path):
            return pcmu_path

    return None


async def play_chime(request):
    """Play audio through the doorbell backchannel.

    If no file parameter given, uses the configured doorbell chime.
    Accepts audio IDs (resolved from chimes/ then audio/) or filenames with extension.
    """
    proxy = request.app.get("rtsp_proxy")
    if not proxy:
        return web.json_response({"error": "RTSP proxy not configured"}, status=503)

    audio_id = request.query.get("file", "")
    if not audio_id:
        config = load_json(CHIME_CONFIG_FILE, {"file": ""})
        audio_id = config.get("file", "")
    if not audio_id:
        return web.json_response({"error": "no chime configured"}, status=400)

    # Strip extension if provided
    if "." in audio_id:
        audio_id = os.path.splitext(audio_id)[0]

    pcmu_path = await asyncio.get_event_loop().run_in_executor(
        None, resolve_audio_to_pcmu, audio_id
    )
    if not pcmu_path:
        return web.json_response({"error": f"audio not found: {audio_id}"}, status=404)

    # inject_chime expects a filename relative to AUDIO_DIR, but now we have
    # files in subdirs. Pass the full path and let the proxy handle it.
    result = await proxy.inject_chime_path(pcmu_path)
    if result == "ok":
        return web.json_response({"status": "ok", "file": audio_id})
    return web.json_response({"error": result}, status=500)


async def on_startup(app):
    app["client_session"] = aiohttp.ClientSession()
    # Start RTSP proxy if configured
    if CAMERA_HOST and CAMERA_STREAM:
        from rtsp_proxy import RtspProxy

        proxy = RtspProxy(
            camera_host=CAMERA_HOST,
            camera_port=CAMERA_PORT,
            camera_user=CAMERA_USER,
            camera_pass=CAMERA_PASS,
            camera_stream=CAMERA_STREAM,
            listen_port=RTSP_PROXY_PORT,
        )
        await proxy.start()
        app["rtsp_proxy"] = proxy
    else:
        print("RTSP proxy: not configured (set CAMERA_HOST and CAMERA_STREAM)")


async def on_cleanup(app):
    await app["client_session"].close()
    proxy = app.get("rtsp_proxy")
    if proxy:
        await proxy.stop()


DOORBELL_PAGE = os.path.join(SERVE_DIR, "webrtc-doorbell.html")


async def serve_doorbell(request):
    """Serve the doorbell page with the theming origin allowlist injected."""
    with open(DOORBELL_PAGE, encoding="utf-8") as f:
        html = f.read()
    return web.Response(
        text=inject_theme_origins(html, THEME_ORIGINS), content_type="text/html"
    )


app = web.Application(middlewares=[common_middleware])
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)
app.router.add_get("/api/cast", cast_trigger)
app.router.add_get("/api/messages", list_messages)
app.router.add_post("/api/messages", create_message)
app.router.add_delete("/api/messages/{name}", delete_message)
app.router.add_get("/api/slots", get_slots)
app.router.add_put("/api/slots", set_slots)
app.router.add_get("/api/chimes", list_chimes)
app.router.add_get("/api/chime-config", get_chime_config)
app.router.add_put("/api/chime-config", set_chime_config)
app.router.add_post("/api/chime", play_chime)
app.router.add_get("/api/go2rtc/api/ws", ws_proxy)
app.router.add_route("*", "/api/go2rtc/{path:.*}", http_proxy)
app.router.add_get("/webrtc-doorbell.html", serve_doorbell)
app.router.add_get("/webrtc-doorbell.html/", serve_doorbell)
app.router.add_static("/", SERVE_DIR, show_index=True)

if __name__ == "__main__":
    runner = web.AppRunner(app)

    async def start():
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        print(f"Listening on http://0.0.0.0:{PORT}")
        print(f"Proxying go2rtc to {GO2RTC}")

    loop = asyncio.new_event_loop()
    loop.run_until_complete(start())
    loop.run_forever()
