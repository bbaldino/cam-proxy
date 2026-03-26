# cam-proxy

A proxy server for IP cameras with two-way audio, designed to work alongside [go2rtc](https://github.com/AlexxIT/go2rtc) and [Home Assistant](https://www.home-assistant.io/). Provides RTSP backchannel mediation, WebRTC doorbell UI, Chromecast casting via DashCast, and doorbell chime/message injection.

## What it does

- **RTSP proxy with backchannel** — sits between go2rtc and the camera, mediating the single backchannel connection so both WebRTC talk-button audio and server-injected chime audio can coexist
- **Doorbell chime injection** — plays configurable chime sounds through the camera's speaker via RTSP backchannel (PCMU/G.711 at 8kHz)
- **Doorbell WebRTC page** — live video with 2-way audio (push-to-talk), pre-recorded quick reply messages, and an optional debug/stats overlay
- **Quick reply message management** — generate TTS audio clips via Google TTS and assign them to doorbell response slots
- **Proxies go2rtc** so browsers can reach it through a reverse proxy (HTTPS for microphone access is handled externally, e.g. via Cloudflare or nginx)
- **Casts to Chromecasts** via DashCast — trigger an HTTP endpoint to load a camera stream on a Google display
- **Home Assistant custom card** (`doorbell-card.js`) — embeds the doorbell page in a Lovelace dashboard

## Files

| File | Description |
|---|---|
| `server2.py` | Main server — HTTP API, go2rtc proxy, Chromecast DashCast, chime/message management |
| `rtsp_proxy.py` | RTSP proxy with backchannel support and chime injection |
| `webrtc-doorbell.html` | Doorbell WebRTC client with 2-way audio and quick replies |
| `cast.html` | Simpler MSE-based player for Chromecast displays |
| `doorbell-card.js` | HA custom Lovelace card (iframe wrapper with loading spinner) |
| `messages.html` | Admin UI for managing chime selection, quick reply TTS clips, and slot assignments |
| `Dockerfile` | Container build |

## Setup

### Prerequisites

- A running [go2rtc](https://github.com/AlexxIT/go2rtc) instance with camera streams configured
- A reverse proxy with HTTPS (e.g. Cloudflare, nginx, traefik) — browser microphone access requires a secure context
- Docker (recommended) or Python 3.12+
- The container must use host networking for Chromecast discovery (mDNS)

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GO2RTC_HOST` | `localhost:1984` | go2rtc host:port to proxy to |
| `PORT` | `8899` | HTTP listen port |
| `CAMERA_HOST` | (required) | Camera IP address |
| `CAMERA_PORT` | `554` | Camera RTSP port |
| `CAMERA_USER` | (required) | Camera RTSP username |
| `CAMERA_PASS` | (required) | Camera RTSP password |
| `CAMERA_STREAM` | (required) | Camera RTSP stream path (e.g. `h264Preview_01_sub`) |
| `RTSP_PROXY_PORT` | `8554` | RTSP proxy listen port (go2rtc connects here) |
| `AUDIO_DIR` | `/app/audio` | Directory for chime and message audio files |
| `CORS_ORIGINS` | (empty) | Comma-separated allowed CORS origins |

### Docker

```bash
docker run -d --network=host \
  -e GO2RTC_HOST=your-go2rtc-host:1984 \
  -e CAMERA_HOST=192.168.1.100 \
  -e CAMERA_USER=admin \
  -e CAMERA_PASS=yourpassword \
  -e CAMERA_STREAM=h264Preview_01_sub \
  -e CORS_ORIGINS=https://your-ha-instance:8123 \
  -v ./audio:/app/audio \
  ghcr.io/bbaldino/cam-proxy:latest
```

### Without Docker

```bash
pip install aiohttp pychromecast gtts
python server2.py
```

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/cast?device=Name&url=URL` | Cast a URL to a Chromecast via DashCast |
| `GET` | `/api/chimes` | List available doorbell chime sounds |
| `GET` | `/api/chime-config` | Get the current active chime |
| `PUT` | `/api/chime-config` | Set the active chime (`{"file": "chime-id"}`) |
| `POST` | `/api/chime?file=ID` | Play a chime (or the active chime if no file given) |
| `GET` | `/api/messages` | List all TTS clips |
| `POST` | `/api/messages` | Create a TTS clip (`{"text": "..."}`) |
| `DELETE` | `/api/messages/{name}` | Delete a clip |
| `GET` | `/api/slots` | Get ordered quick reply slots |
| `PUT` | `/api/slots` | Set slot order (`["clip-id", ...]`) |
| `*` | `/api/go2rtc/*` | Proxied to go2rtc |

## Audio Files

Chime and message audio files are stored in the `AUDIO_DIR` volume:

- `chimes/*.mp3` — doorbell chime source files (place MP3s here)
- `*.mp3` — quick reply message audio (managed via API/UI)
- `*.json` — message metadata and slot configuration
- `.pcmu` files are auto-generated from MP3s on first use via ffmpeg

## WebRTC Doorbell Page

`webrtc-doorbell.html` accepts these URL parameters:

| Parameter | Default | Description |
|---|---|---|
| `stream` | `doorbell_sub` | go2rtc stream name |
| `go2rtc` | `/api/go2rtc` | go2rtc API base path (proxied) |
| `talk` | `1` | Set to `0` to hide the talk button (e.g. for Chromecast displays without mic access) |
| `debug` | `0` | Set to `1` for always-on debug overlay with connection timing and media stats |

## Home Assistant Integration

### Doorbell card

Add `doorbell-card.js` as a Lovelace resource, then use in a card or browser_mod popup:

```yaml
type: custom:doorbell-card
url: https://your-cam-proxy-host/webrtc-doorbell.html
height: 80vh
```

### Chime trigger via REST command

```yaml
rest_command:
  doorbell_chime:
    url: "http://cam-proxy-host:8899/api/chime"
    method: POST
```

### Cast trigger via REST command

```yaml
rest_command:
  cast_doorbell:
    url: "http://cam-proxy-host:8899/api/cast?device=Office+display"
    method: GET
```
