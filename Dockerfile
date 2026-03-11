FROM python:3.12-slim
RUN pip install --no-cache-dir aiohttp pychromecast gtts
WORKDIR /app
COPY server2.py cast.html webrtc-doorbell.html messages.html doorbell-card.js ./
CMD ["python", "-u", "server2.py"]
