#!/usr/bin/env python3
"""HTTP + WebSocket proxy server for go2rtc with DashCast trigger."""
import asyncio
import json
import os
import re
import aiohttp
from aiohttp import web
import pychromecast
from pychromecast.controllers.dashcast import DashCastController
from gtts import gTTS

GO2RTC = os.environ.get("GO2RTC_HOST", "localhost:1984")
SERVE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8899"))


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
                forward_to_server(),
                forward_to_client(),
                return_exceptions=True
            )

    return ws_client


async def http_proxy(request):
    """Proxy HTTP requests to go2rtc."""
    import time
    t0 = time.monotonic()
    path = request.path[len("/api/go2rtc"):]
    target = f"http://{GO2RTC}{path}"
    if request.query_string:
        target += f"?{request.query_string}"

    body = await request.read()
    t1 = time.monotonic()
    session = request.app["client_session"]
    async with session.request(
        request.method, target,
        data=body if body else None,
        headers={"Content-Type": request.content_type} if request.content_type else {}
    ) as resp:
        data = await resp.read()
        t2 = time.monotonic()
        print(f"Proxy {request.method} {path} -> read body: {(t1-t0)*1000:.0f}ms, go2rtc: {(t2-t1)*1000:.0f}ms, total: {(t2-t0)*1000:.0f}ms")
        return web.Response(
            body=data,
            status=resp.status,
            content_type=resp.content_type
        )






async def cast_trigger(request):
    """Trigger DashCast to load a URL on a Chromecast."""
    device = request.query.get("device", "Office display")
    stream = request.query.get("stream", "doorbell_sub")
    host = request.host.split(":")[0] if request.host else "localhost"
    url = request.query.get("url", f"http://{host}:{PORT}/cast.html?stream={stream}")

    def do_cast():
        chromecasts, browser = pychromecast.get_listed_chromecasts(friendly_names=[device])
        if not chromecasts:
            browser.stop_discovery()
            return f"Device '{device}' not found"
        cast = chromecasts[0]
        cast.wait()
        d = DashCastController()
        cast.register_handler(d)
        d.load_url(url, force=True)
        import time
        time.sleep(5)
        browser.stop_discovery()
        cast.disconnect()
        return f"Cast sent: {url} -> {device}"

    result = await asyncio.get_event_loop().run_in_executor(None, do_cast)
    print(result)
    return web.Response(text=result)


AUDIO_DIR = os.path.join(SERVE_DIR, "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
MESSAGES_META = os.path.join(AUDIO_DIR, "messages.json")
SLOTS_FILE = os.path.join(AUDIO_DIR, "slots.json")


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:50]


def get_message_info(msg_id, meta):
    return {
        "id": msg_id,
        "text": meta.get(msg_id, {}).get("text", msg_id),
        "file": f"audio/{msg_id}.mp3",
    }


async def list_messages(request):
    """List all available pre-recorded messages."""
    meta = load_json(MESSAGES_META, {})
    messages = []
    for f in sorted(os.listdir(AUDIO_DIR)):
        if f.endswith('.mp3'):
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
        path = os.path.join(AUDIO_DIR, f"{name}.mp3")
        tts.save(path)
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


CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]


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


async def on_startup(app):
    app["client_session"] = aiohttp.ClientSession()


async def on_cleanup(app):
    await app["client_session"].close()

app = web.Application(middlewares=[common_middleware])
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)
app.router.add_get("/api/cast", cast_trigger)
app.router.add_get("/api/messages", list_messages)
app.router.add_post("/api/messages", create_message)
app.router.add_delete("/api/messages/{name}", delete_message)
app.router.add_get("/api/slots", get_slots)
app.router.add_put("/api/slots", set_slots)
app.router.add_get("/api/go2rtc/api/ws", ws_proxy)
app.router.add_route("*", "/api/go2rtc/{path:.*}", http_proxy)
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
