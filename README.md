# Social Whisper Transcript MCP

**Turn any YouTube, Instagram, TikTok, X (Twitter), Facebook, Vimeo or podcast link into a full transcript, on your own server, with OpenAI Whisper. Built for AI agents.**

A self-hosted **MCP server + REST API** in one Docker container. Give it a video URL. It downloads the audio
with [yt-dlp](https://github.com/yt-dlp/yt-dlp), transcribes it with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
(Whisper `large-v3-turbo` / `large-v3`), and hands back the text, timestamps, subtitles or a ready-to-download file.
Works with **Claude Code, Claude Desktop, Cursor, VS Code, OpenClaw** and any other MCP client.

[![CI](https://github.com/vireshmahajan/social-whisper-transcript-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/vireshmahajan/social-whisper-transcript-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

```text
You:    "Summarise this: https://www.instagram.com/reel/..."
Agent:  transcribe_url(url) -> full Whisper transcript -> summary
```

## Why this instead of a YouTube-transcript tool?

Most transcript MCP servers just fetch YouTube's own captions. That breaks when:

- the video has **no captions**, or only bad auto-captions (common for Hindi and other non-English videos, music, accents, names)
- the link is **Instagram, TikTok, X, Facebook, Vimeo, a podcast**, or any of yt-dlp's ~1800 other sites
- you need **timestamps, subtitles (SRT/VTT)** or a file to hand over

This server transcribes the actual audio with Whisper. It runs on hardware you already have (a NAS, homelab box or VPS, no GPU needed),
so there are **no per-minute API costs** and the audio never leaves your network.

## Features

- **Any link**: YouTube, Shorts, Instagram Reels, TikTok, X/Twitter, Facebook, Vimeo, SoundCloud, podcasts… (yt-dlp updates itself on every start)
- **Whisper large-v3-turbo** by default (fast), **large-v3** for best accuracy; 99 languages with auto-detection
- **MCP over Streamable HTTP** with 4 tools, plus a plain **REST API** with Swagger docs at `/docs`
- **Job queue**: long videos (up to 4 h) run in the background; agents wait or check back, nothing times out
- **Readable output**: paragraphs with `[m:ss]` timestamps, Markdown, SRT, VTT or JSON with every segment
- **File downloads**: a 24-hour **signed link** an agent can pass on or `curl` without the API token
- **Reuse**: asking for the same URL again returns the earlier transcript instantly
- **Light when idle**: models load on the first job and unload after 10 minutes (~50 MB RAM idle)
- **Music / singing**: falls back to full-audio transcription when voice detection finds nothing
- Bearer-token auth, refuses links into your own network (SSRF guard), runs as non-root

## Quick start (Docker Compose)

```bash
mkdir social-whisper-transcript && cd social-whisper-transcript
curl -fsSLO https://raw.githubusercontent.com/vireshmahajan/social-whisper-transcript-mcp/main/docker-compose.yml
echo "API_TOKEN=$(openssl rand -hex 24)" > .env
docker compose up -d
curl http://localhost:8000/health
```

The first transcription downloads the Whisper model (~1.6 GB for turbo) into the `transcript-data` volume.

<details>
<summary>Plain <code>docker run</code> instead</summary>

```bash
docker run -d --name social-whisper-transcript --restart unless-stopped \
  -p 8000:8000 -e API_TOKEN=$(openssl rand -hex 24) \
  -v transcript-data:/data \
  ghcr.io/vireshmahajan/social-whisper-transcript-mcp:latest
```
</details>

Try it:

```bash
TOKEN=$(grep API_TOKEN .env | cut -d= -f2)
curl -s -X POST http://localhost:8000/transcribe \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "wait": true}' | jq -r .text
```

## Connect your AI agent

The MCP endpoint is `http://<your-server>:8000/mcp` (Streamable HTTP) with the header `Authorization: Bearer <API_TOKEN>`.

**Claude Code**

```bash
claude mcp add --scope user --transport http transcript http://your-server:8000/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

**Cursor** (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "transcript": {
      "url": "http://your-server:8000/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`)

```json
{
  "servers": {
    "transcript": {
      "type": "http",
      "url": "http://your-server:8000/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

**Claude Desktop** (via [mcp-remote](https://www.npmjs.com/package/mcp-remote), `claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "transcript": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://your-server:8000/mcp", "--allow-http",
               "--header", "Authorization: Bearer YOUR_TOKEN"]
    }
  }
}
```

**OpenClaw**

```bash
echo 'TRANSCRIPT_TOKEN=YOUR_TOKEN' >> ~/.openclaw/.env
openclaw mcp set transcript '{"url":"http://your-server:8000/mcp","transport":"streamable-http",
  "headers":{"Authorization":"Bearer ${TRANSCRIPT_TOKEN}"}}'
```

Set `"transport": "streamable-http"` explicitly (OpenClaw defaults to SSE). The optional [skill](skills/social-whisper-transcript/SKILL.md)
tells the agent when and how to use the tools.

## MCP tools

| Tool | What it does |
|---|---|
| `transcribe_url(url, language?, model?, wait_seconds=50, output="text")` | Starts (or reuses) a transcription. Short clips return the transcript directly; long ones return a job id. `output`: `text`, `timestamped`, `srt`. |
| `get_transcript(job_id, output?, wait_seconds=0, offset=0)` | Status or transcript of a job; waits up to 10 min; pages long transcripts. |
| `get_transcript_file(job_id, format="txt", wait_seconds=0)` | The finished transcript as a file (`txt`, `md`, `srt`, `vtt`, `json`), inline plus a 24 h download link that needs no token. Says "still running" until done. |
| `list_transcripts(limit=20)` | Recent jobs with ids, status, title and length. |

## REST API

All endpoints except `/health` need `Authorization: Bearer <API_TOKEN>`. Interactive docs: `http://your-server:8000/docs`.

| Endpoint | |
|---|---|
| `POST /transcribe` `{"url", "language"?, "model"?, "wait"?}` | Queue a job (`202` + id), or with `"wait": true` block until done (max 10 min) |
| `GET /jobs/{id}` | Status, progress, and the full result (text + segments) when done |
| `GET /jobs/{id}/transcript?format=txt\|srt\|vtt\|json` | Raw transcript |
| `GET /jobs/{id}/file?format=txt\|md\|srt\|vtt\|json` | Download as a file named after the video. `202` + `Retry-After` while running, `409` if it failed |
| `GET /jobs` | Recent jobs |
| `GET /health` | Liveness, loaded models, queue length |

Example `.txt` file:

```text
Title:       Steve Jobs' 2005 Stanford Commencement Address
By:          Stanford (Youtube)
Length:      15:04
Language:    en (100% sure)
Model:       Whisper large-v3-turbo
------------------------------------------------------------

[0:20] Thank you. I'm honored to be with you today for your commencement …

[0:47] Today, I want to tell you three stories from my life. That's it. No big deal …
```

## Models, speed and memory

Measured on an Intel i9-11900 (8 threads, CPU only, int8):

| Model | Speed | 1 h of audio | RAM while working | Best for |
|---|---|---|---|---|
| `large-v3-turbo` (default) | ~7× real time | ~9 min | ~2 GB | Most videos |
| `large-v3` | ~2.6× real time | ~23 min | ~4 GB | Non-English, accents, names, lyrics |
| `medium`, `small` | faster | | less | Weak hardware |

Jobs run one at a time and use `WHISPER_THREADS` cores. With nothing to do, models unload after `IDLE_UNLOAD_SECONDS`.

## Configuration

| Variable | Default | |
|---|---|---|
| `API_TOKEN` | (empty = no auth, don't do that) | Bearer token for the REST API and MCP; also signs download links |
| `WHISPER_MODEL` | `large-v3-turbo` | `large-v3-turbo`, `large-v3`, `medium`, `small` |
| `WHISPER_THREADS` | CPUs, max 8 | CPU threads per job |
| `MAX_DURATION_SECONDS` | `14400` | Longest video accepted (4 h) |
| `IDLE_UNLOAD_SECONDS` | `600` | Free model RAM after this idle time; `0` = keep loaded |
| `PRELOAD_MODEL` | `false` | Load the default model at startup |
| `WAIT_TIMEOUT_SECONDS` | `600` | Max blocking time for `"wait": true` |
| `PUBLIC_BASE_URL` | (address the client used) | Base for download links, e.g. behind a reverse proxy or tunnel |
| `DOWNLOAD_LINK_TTL_SECONDS` | `86400` | How long signed download links work |
| `ALLOW_PRIVATE_URLS` | `false` | Allow links to localhost / LAN addresses |

**Login-only posts** (private Instagram/Facebook, age-restricted YouTube): export cookies from a logged-in browser in Netscape
format (e.g. with a "Get cookies.txt" extension) and copy the file to `/data/cookies.txt` in the volume:
`docker cp cookies.txt social-whisper-transcript:/data/cookies.txt`.

## Security notes

- **Always set `API_TOKEN`.** Without it, anyone who can reach the port can use your CPU.
- Meant for your LAN or a VPN such as Tailscale. To expose it to the internet, put it behind HTTPS (Caddy, Traefik, Cloudflare Tunnel) and set `PUBLIC_BASE_URL`.
- MCP DNS-rebinding protection is off so LAN and Tailscale hostnames work; the bearer token is the guard.
- Links that resolve to private, loopback or link-local addresses are refused unless `ALLOW_PRIVATE_URLS=true`.

## Troubleshooting

- **"Sign in to confirm you're not a bot" / a site stopped working**: restart the container (`docker restart social-whisper-transcript`), which updates yt-dlp. For logins, add `cookies.txt`.
- **Empty transcript on music videos**: handled automatically (retries without voice detection); `large-v3` does better with lyrics.
- **Wrong language detected**: pass `language` (`"hi"`, `"de"`, …).
- **Slow**: use `large-v3-turbo`, raise `WHISPER_THREADS`, or give the container more CPU.

## Development

```bash
pip install -r requirements-dev.txt
pytest                       # no network, model or ffmpeg needed
docker build -t social-whisper-transcript-mcp .
```

Everything lives in [`app.py`](app.py): FastAPI + the official MCP Python SDK (FastMCP), one worker thread, jobs stored as JSON in `/data/jobs`.

## Legal

You are responsible for having the right to download and transcribe the content you send to this server, and for complying
with each site's terms of service. This project is not affiliated with OpenAI, YouTube, Meta, TikTok or X.

## License

[MIT](LICENSE) © Viresh Mahajan
