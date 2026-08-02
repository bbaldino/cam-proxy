FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir aiohttp pychromecast gtts
WORKDIR /app
COPY server2.py rtsp_proxy.py theme_origins.py cast.html webrtc-doorbell.html messages.html doorbell-card.js test-embed.html ./
EXPOSE 8899 8554
VOLUME /app/audio
CMD ["python", "-u", "server2.py"]
