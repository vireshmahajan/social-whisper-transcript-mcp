FROM python:3.12-slim

LABEL org.opencontainers.image.title="social-whisper-transcript-mcp" \
      org.opencontainers.image.description="Self-hosted MCP server + REST API: YouTube/Instagram/TikTok/X/podcast URL to Whisper transcript" \
      org.opencontainers.image.source="https://github.com/vireshmahajan/social-whisper-transcript-mcp" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py entrypoint.sh ./

# /data holds jobs, downloaded models and the self-updated yt-dlp; owned by the runtime user
RUN mkdir -p /data && chown 1000:1000 /data
ENV PYTHONUNBUFFERED=1 HOME=/data DATA_DIR=/data
USER 1000:1000
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
ENTRYPOINT ["/app/entrypoint.sh"]
