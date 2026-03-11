# cast-proxy

A proxy server for casting camera streams to Chromecasts and serving an interactive doorbell WebRTC interface, designed to work alongside [go2rtc](https://github.com/AlexxIT/go2rtc) and [Home Assistant](https://www.home-assistant.io/).

## What it does

- **Proxies go2rtc** so browsers can reach it through a reverse proxy (HTTPS for microphone access is handled externally, e.g. via Cloudflare or nginx)
- **Casts to Chromecasts** via DashCast — trigger an HTTP endpoint to load a camera stream on a Google display
- **Doorbell WebRTC page** — live video with 2-way audio (push-to-talk), pre-recorded quick reply messages, a stats overlay, and fast cold-start via stream pre-warming
- **Quick reply message management** — generate TTS audio clips via Google TTS and assign them to doorbell response slots
- **Home Assistant custom card** (`doorbell-card.js`) — embeds the doorbell page in a Lovelace dashboard

## Files

| File | Description |
|---|---|
| `server2.py` | Main server — go2rtc proxy, Chromecast DashCast trigger, message CRUD API |
| `webrtc-doorbell.html` | Doorbell WebRTC client with 2-way audio and quick replies |
| `cast.html` | Simpler MSE-based player for Chromecast displays |
| `doorbell-card.js` | HA custom Lovelace card (iframe wrapper with loading spinner) |
| `messages.html` | Admin UI for managing quick reply TTS clips and slot assignments |
| `Dockerfile` | Container build |

## Setup

### Prerequisites

- A running [go2rtc](https://github.com/AlexxIT/go2rtc) instance with camera streams configured
- A reverse proxy with HTTPS (e.g. Cloudflare, nginx, traefik) — browser microphone access requires a secure context
- Docker (recommended) or Python 3.12+

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GO2RTC_HOST` | `localhost:1984` | go2rtc host:port to proxy to |
| `PORT` | `8899` | HTTP listen port |
| `CORS_ORIGINS` | (empty) | Comma-separated allowed CORS origins |

### Docker

```bash
docker build -t cast-proxy .
docker run -d \
  -p 8899:8899 \
  -e GO2RTC_HOST=your-go2rtc-host:1984 \
  -e CORS_ORIGINS=https://your-ha-instance:8123 \
  -v ./audio:/app/audio \
  cast-proxy
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
| `GET` | `/api/messages` | List all TTS clips |
| `POST` | `/api/messages` | Create a TTS clip (`{"text": "..."}`) |
| `DELETE` | `/api/messages/{name}` | Delete a clip |
| `GET` | `/api/slots` | Get ordered quick reply slots |
| `PUT` | `/api/slots` | Set slot order (`["clip-id", ...]`) |
| `*` | `/api/go2rtc/*` | Proxied to go2rtc |

## WebRTC Doorbell Page

`webrtc-doorbell.html` accepts these URL parameters:

| Parameter | Default | Description |
|---|---|---|
| `stream` | `doorbell_sub` | go2rtc stream name |
| `go2rtc` | `/api/go2rtc` | go2rtc API base path (proxied) |
| `talk` | `1` | Set to `0` to hide the talk button (e.g. for Chromecast displays without mic access) |

The page pre-warms the ffmpeg opus producer on load by briefly requesting an MP4 stream, reducing cold-start connection time from ~5-7s to ~1.5s.

## Home Assistant Integration

### Doorbell card

Add `doorbell-card.js` as a Lovelace resource, then use in a card or browser_mod popup:

```yaml
type: custom:doorbell-card
url: https://your-cast-proxy-host/webrtc-doorbell.html
height: 80vh
```

### Cast trigger via REST command

```yaml
rest_command:
  cast_doorbell:
    url: "http://cast-proxy-host:8899/api/cast?device=Office+display&url=https://your-domain/webrtc-doorbell.html%3Ftalk%3D0"
```
