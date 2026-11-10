"""Social Whisper Transcript MCP: video/audio URL -> yt-dlp -> faster-whisper -> transcript.

https://github.com/vireshmahajan/social-whisper-transcript-mcp  (MIT licence)

POST /transcribe {"url": "...", "language": "en"|"hi"|null, "model": "large-v3"|null, "wait": false}
GET  /jobs/{id}                         status + progress; full result (text + segments) when done
GET  /jobs/{id}/transcript?format=txt   txt | srt | vtt | json
GET  /jobs/{id}/file?format=txt         download as a file: txt (readable, default) | md | srt | vtt | json
                                        202 + "still running" until the job is done
GET  /jobs                              recent jobs
GET  /health                            no auth
POST /mcp                               MCP server (streamable HTTP) for agents: transcribe_url,
                                        get_transcript, get_transcript_file, list_transcripts

Jobs run one at a time (whisper already uses all the threads it is given), are stored as
JSON in /data/jobs and survive restarts. Auth: "Authorization: Bearer $API_TOKEN".
"""
import asyncio
import glob
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, HttpUrl

__version__ = "1.1.0"

DATA = Path(os.environ.get("DATA_DIR", "/data"))
JOBS_DIR = DATA / "jobs"
WORK_DIR = DATA / "work"
MODELS_DIR = DATA / "models"
COOKIES = DATA / "cookies.txt"  # optional, Netscape format, for login-only posts (FB/Instagram)
for d in (JOBS_DIR, WORK_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)

API_TOKEN = os.environ.get("API_TOKEN", "")
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
THREADS = int(os.environ.get("WHISPER_THREADS", str(min(8, os.cpu_count() or 4))))
MAX_DURATION = int(os.environ.get("MAX_DURATION_SECONDS", str(4 * 3600)))
WAIT_TIMEOUT = int(os.environ.get("WAIT_TIMEOUT_SECONDS", "600"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")  # for download links behind a proxy
LINK_TTL = int(os.environ.get("DOWNLOAD_LINK_TTL_SECONDS", "86400"))
IDLE_UNLOAD = int(os.environ.get("IDLE_UNLOAD_SECONDS", "600"))  # free model RAM after this long without jobs; 0 = never
ALLOW_PRIVATE_URLS = os.environ.get("ALLOW_PRIVATE_URLS", "false").lower() == "true"
ModelName = Literal["large-v3-turbo", "large-v3", "medium", "small"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("transcript")
for noisy in ("httpx", "huggingface_hub"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_app):
    if not API_TOKEN:
        log.warning("API_TOKEN is not set: anyone who can reach this server can use it. Set one!")
    _recover_interrupted()
    threading.Thread(target=worker, daemon=True, name="worker").start()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Social Whisper Transcript", version=__version__, lifespan=lifespan,
              description="Send a YouTube / Instagram / TikTok / X / Facebook / podcast URL, get a Whisper "
                          "transcript back. Agents: use the MCP endpoint at /mcp (same bearer token).")


def auth(authorization: str | None = Header(default=None)) -> None:
    if not API_TOKEN:
        return
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {API_TOKEN}"):
        raise HTTPException(401, "Missing or wrong token. Send 'Authorization: Bearer <API_TOKEN>'.")


# ---------------------------------------------------------------- job store
_lock = threading.Lock()
_active: dict[str, dict] = {}       # queued / running jobs (live progress)
_events: dict[str, threading.Event] = {}
_queue: "queue.Queue[str]" = queue.Queue()


def _path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _save(job: dict) -> None:
    tmp = _path(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False))
    tmp.replace(_path(job["id"]))


def _update(job_id: str, **fields) -> dict:
    with _lock:
        job = _active[job_id]
        job.update(fields)
        return job


def get_job(job_id: str) -> dict:
    try:
        uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(404, "No such job") from None
    with _lock:
        if job_id in _active:
            return dict(_active[job_id], queue_position=_queue_position(job_id))
    p = _path(job_id)
    if not p.exists():
        raise HTTPException(404, "No such job")
    return json.loads(p.read_text())


def _queue_position(job_id: str) -> int | None:
    if _active.get(job_id, {}).get("status") != "queued":
        return None
    queued = [j for j in _active.values() if j["status"] == "queued"]
    queued.sort(key=lambda j: j["created_at"])
    return next(i for i, j in enumerate(queued, 1) if j["id"] == job_id)


def _recover_interrupted() -> None:
    for p in JOBS_DIR.glob("*.json"):
        job = json.loads(p.read_text())
        if job.get("status") not in ("done", "error"):
            job.update(status="error", error="Interrupted by a restart; submit the URL again.")
            _save(job)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- whisper
_models: dict[str, object] = {}


def get_model(name: str):
    if name not in _models:
        from faster_whisper import WhisperModel

        log.info("loading model %s (%d threads)", name, THREADS)
        _models[name] = WhisperModel(name, device="cpu", compute_type="int8",
                                     cpu_threads=THREADS, download_root=str(MODELS_DIR))
    return _models[name]


def release_memory() -> None:
    """Hand freed memory (models, audio buffers) back to the OS instead of keeping it in the heap."""
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


def unload_models() -> None:
    names = list(_models)
    _models.clear()
    release_memory()
    log.info("no jobs for %ds: unloaded %s", IDLE_UNLOAD, ", ".join(names))


def load_audio(path: Path):
    """Decode any audio/video file to 16 kHz mono float32 with ffmpeg (avoids PyAV API breakage)."""
    import numpy as np

    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                        "-ar", "16000", "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("ffmpeg could not decode the audio: " + r.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(r.stdout, np.float32)


# ---------------------------------------------------------------- download
def check_url(url: str) -> None:
    """Refuse links to this server's own network (localhost, LAN, cloud metadata) unless allowed."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    host = urlparse(url).hostname
    if not host:
        raise ValueError("url has no host name")
    if ALLOW_PRIVATE_URLS:
        return
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return  # unknown host: let yt-dlp report it
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global:
            raise ValueError(f"{host} is a private or local address. Set ALLOW_PRIVATE_URLS=true to allow it.")


def download(job_id: str, url: str, workdir: Path) -> tuple[Path, dict]:
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading" and d.get("total_bytes"):
            _update(job_id, progress=round(0.1 * d["downloaded_bytes"] / d["total_bytes"], 3))

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "media.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") in ("playlist", "multi_video"):
            raise ValueError("That URL is a playlist or channel. Send the URL of a single video.")
        duration = info.get("duration") or 0
        if duration > MAX_DURATION:
            raise ValueError(f"Video is {duration // 60} min long; the limit is {MAX_DURATION // 60} min.")
        _update(job_id, title=info.get("title"), uploader=info.get("uploader"),
                duration=duration or None, platform=info.get("extractor_key"))
        ydl.process_ie_result(info, download=True)
    files = [Path(f) for f in glob.glob(str(workdir / "media.*")) if not f.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("Download finished but no media file was written.")
    return files[0], info


# ---------------------------------------------------------------- worker
def run_job(job_id: str) -> None:
    job = _update(job_id, status="downloading", started_at=time.time(), progress=0.0)
    workdir = WORK_DIR / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        media, _ = download(job_id, job["url"], workdir)
        model_name = job["model"]
        if model_name not in _models:
            _update(job_id, status="loading_model", progress=0.1)
        model = get_model(model_name)
        _update(job_id, status="transcribing", progress=0.1)
        t0 = time.time()
        audio = load_audio(media)

        def run(vad: bool):
            segments_iter, info = model.transcribe(audio, language=job["language"], vad_filter=vad, beam_size=5)
            segs = []
            for s in segments_iter:
                segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()})
                if info.duration:
                    _update(job_id, progress=round(0.1 + 0.9 * min(s.end / info.duration, 1.0), 3))
            return segs, info

        # Voice detection skips silence (faster, fewer hallucinations) but can drop speech or
        # singing over loud music; if it finds nothing, go again over the whole audio.
        segments, info = run(vad=True)
        if not any(s["text"] for s in segments):
            log.info("job %s: no speech found with voice detection, retrying without it", job_id)
            _update(job_id, progress=0.1)
            segments, info = run(vad=False)
        took = time.time() - t0
        _update(job_id, status="done", progress=1.0, finished_at=time.time(),
                language=info.language, language_probability=round(info.language_probability, 3),
                duration=round(info.duration, 1), transcribe_seconds=round(took, 1),
                speed_x_realtime=round(info.duration / took, 2) if took else None,
                text=" ".join(s["text"] for s in segments if s["text"]), segments=segments)
    except Exception as e:  # noqa: BLE001 - report every failure to the caller
        msg = str(e).replace("\x1b[0;31mERROR:\x1b[0m ", "").strip() or type(e).__name__
        log.warning("job %s failed: %s", job_id, msg)
        _update(job_id, status="error", error=msg[:1000], finished_at=time.time())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        with _lock:
            job = _active.pop(job_id)
            _save(job)
        _events.pop(job_id, threading.Event()).set()


def worker() -> None:
    if os.environ.get("PRELOAD_MODEL", "false") == "true":
        try:
            get_model(DEFAULT_MODEL)
        except Exception:  # noqa: BLE001
            log.exception("preloading %s failed; will retry on first job", DEFAULT_MODEL)
    while True:
        try:
            job_id = _queue.get(timeout=IDLE_UNLOAD if (_models and IDLE_UNLOAD > 0) else None)
        except queue.Empty:
            unload_models()
            continue
        run_job(job_id)
        release_memory()


# ---------------------------------------------------------------- API
class TranscribeRequest(BaseModel):
    url: HttpUrl
    language: str | None = Field(None, description="ISO code such as 'en', 'hi', 'de'. Empty = auto-detect.")
    model: ModelName | None = Field(None, description="Default large-v3-turbo. large-v3 = most accurate, ~4x slower.")
    wait: bool = Field(False, description=f"Block until done (max {WAIT_TIMEOUT}s), for short clips.")


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "default_model": DEFAULT_MODEL,
            "loaded_models": list(_models), "queued": _queue.qsize()}


def submit_job(url: str, language: str | None, model: str | None) -> str:
    job_id = str(uuid.uuid4())
    job = {"id": job_id, "url": url, "language": language or None,
           "model": model or DEFAULT_MODEL, "status": "queued", "progress": 0.0,
           "created_at": time.time()}
    with _lock:
        _active[job_id] = job
        _events[job_id] = threading.Event()
    _queue.put(job_id)
    return job_id


@app.post("/transcribe", dependencies=[Depends(auth)], status_code=202)
def transcribe(req: TranscribeRequest):
    try:
        check_url(str(req.url))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    job_id = submit_job(str(req.url), req.language, req.model)
    event = _events.get(job_id)
    if req.wait and event:
        event.wait(WAIT_TIMEOUT)
        result = get_job(job_id)
        return JSONResponse(result, status_code=200 if result["status"] in ("done", "error") else 202)
    return {"id": job_id, "status": "queued", "status_url": f"/jobs/{job_id}",
            "queue_position": _queue_position(job_id)}


@app.get("/jobs", dependencies=[Depends(auth)])
def list_jobs(limit: int = Query(20, le=200)):
    with _lock:
        live = [dict(j) for j in _active.values()]
    done = []
    for p in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        done.append(json.loads(p.read_text()))
    keep = ("id", "status", "url", "title", "duration", "language", "model", "created_at", "progress", "error")
    return [{k: j.get(k) for k in keep} for j in (live + done)[:limit]]


@app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
def job_status(job_id: str):
    return get_job(job_id)


def _ts(sec: float, sep: str) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


@app.get("/jobs/{job_id}/transcript", dependencies=[Depends(auth)])
def transcript(job_id: str, format: Literal["txt", "srt", "vtt", "json"] = "txt"):
    job = get_job(job_id)
    if job["status"] != "done":
        raise HTTPException(409, f"Job is {job['status']}" + (f": {job.get('error')}" if job.get("error") else ""))
    segs = job["segments"]
    if format == "json":
        return segs
    if format == "txt":
        return PlainTextResponse("\n".join(s["text"] for s in segs) + "\n")
    if format == "srt":
        body = "\n".join(f"{i}\n{_ts(s['start'], ',')} --> {_ts(s['end'], ',')}\n{s['text']}\n"
                         for i, s in enumerate(segs, 1))
        return PlainTextResponse(body, media_type="application/x-subrip")
    body = "WEBVTT\n\n" + "\n".join(f"{_ts(s['start'], '.')} --> {_ts(s['end'], '.')}\n{s['text']}\n" for s in segs)
    return PlainTextResponse(body, media_type="text/vtt")


# ---------------------------------------------------------------- MCP (for agents)
from mcp.server.mcpserver import Context, MCPServer  # noqa: E402  (MCP Python SDK 2.x)
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.types import EmbeddedResource, TextContent, TextResourceContents, ToolAnnotations  # noqa: E402
from starlette.routing import Route  # noqa: E402

mcp = MCPServer(
    "social-whisper-transcript",
    version=__version__,
    instructions=(
        "Transcribes speech in online videos and audio (YouTube, Instagram, TikTok, X, Facebook, Vimeo, "
        "podcasts and ~1800 other sites) with Whisper on a self-hosted server. Call transcribe_url with a "
        "link. Short clips come back directly; long ones return a job id - then call get_transcript with "
        "wait_seconds to wait for it. On a typical 8-core CPU a 1-hour video takes ~10 min with the default "
        "model (large-v3-turbo) and ~25 min with large-v3 (most accurate; use it for non-English, accents "
        "or names). "
        "Transcripts of the same URL are reused, so asking again is free. To save or hand over the result "
        "as a file (txt, md, srt, vtt, json), call get_transcript_file: it returns the file and a "
        "download link that works without a token for 24 hours."
    ),
)
_READS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
Output = Literal["text", "timestamped", "srt"]
MAX_WAIT = 600


def _clock(sec: float) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def _job_or_error(job_id: str) -> dict:
    try:
        return get_job(job_id.strip())
    except HTTPException:
        raise ValueError(f"No job with id {job_id!r}. Use list_transcripts to find it.") from None


def _find_existing(url: str, model: str, language: str | None) -> str | None:
    """A queued/running or finished job for the same URL and model, so agents don't redo work."""
    def same(j):
        return (j.get("url") == url and j.get("model") == model
                and (language is None or j.get("language") == language))
    with _lock:
        for j in _active.values():
            if same(j):
                return j["id"]
    for p in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            j = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if j.get("status") == "done" and j.get("text") and same(j):
            return j["id"]
    return None


def _render(job: dict, output: str, offset: int, max_chars: int) -> str:
    jid = job["id"]
    if job["status"] == "error":
        return f"Transcription failed (job {jid}): {job.get('error')}"
    if job["status"] != "done":
        pos = job.get("queue_position")
        where = f", position {pos} in queue" if pos else ""
        return (f"Not finished yet: {job['status']}, {round(100 * (job.get('progress') or 0))}% done{where}. "
                f"Title: {job.get('title') or 'unknown yet'}; length {_clock(job.get('duration'))}.\n"
                f'Job id: {jid}. Call get_transcript(job_id="{jid}", wait_seconds=300) to wait for it.')
    segs = job.get("segments") or []
    if output == "timestamped":
        body = "\n".join(f"[{_clock(s['start'])}] {s['text']}" for s in segs)
    elif output == "srt":
        body = "\n".join(f"{i}\n{_ts(s['start'], ',')} --> {_ts(s['end'], ',')}\n{s['text']}\n"
                         for i, s in enumerate(segs, 1))
    else:
        body = job.get("text") or ""
    if not body.strip():
        body = "(No speech was detected in this audio.)"
    lang = job.get("language") or "?"
    prob = job.get("language_probability")
    header = (f"Title: {job.get('title')}\nBy: {job.get('uploader')} ({job.get('platform')})\nURL: {job['url']}\n"
              f"Length: {_clock(job.get('duration'))} | Language: {lang}"
              f"{f' ({round(prob * 100)}%)' if prob else ''} | Model: {job.get('model')} | Job id: {jid}\n\n")
    offset = max(0, offset)
    end = offset + max(1000, max_chars)
    chunk = body[offset:end]
    if end < len(body):
        chunk += (f"\n\n[Showing characters {offset}-{end} of {len(body)}. "
                  f'Call get_transcript(job_id="{jid}", output="{output}", offset={end}) for the rest.]')
    return header + chunk


async def _wait(job_id: str, wait_seconds: int, ctx: Context | None) -> dict:
    deadline = time.monotonic() + max(0, min(wait_seconds, MAX_WAIT))
    while True:
        job = _job_or_error(job_id)
        if job["status"] in ("done", "error") or time.monotonic() >= deadline:
            return job
        if ctx is not None:
            try:
                await ctx.report_progress(round(100 * (job.get("progress") or 0)), 100)
            except Exception:  # noqa: BLE001 - progress is best effort
                pass
        await asyncio.sleep(2)


@mcp.tool(annotations=ToolAnnotations(title="Transcribe a video/audio URL", readOnlyHint=False,
                                      destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def transcribe_url(
    url: str,
    language: str | None = None,
    model: ModelName | None = None,
    wait_seconds: int = 50,
    output: Output = "text",
    max_chars: int = 40000,
    ctx: Context | None = None,
) -> str:
    """Transcribe the speech in a video or audio URL (YouTube, Facebook, Instagram, TikTok, X, Vimeo, podcasts...).

    Args:
        url: Link to a single video or audio post (not a playlist or channel).
        language: ISO code such as "en", "hi", "de". Leave empty to auto-detect.
        model: "large-v3-turbo" (default, fast) or "large-v3" (most accurate; Hindi, accents, names).
        wait_seconds: How long to wait for the result before returning a job id (0-600).
        output: "text" (plain), "timestamped" (one line per segment with [m:ss]) or "srt".
        max_chars: Maximum transcript characters to return; longer transcripts are paged.
    """
    check_url(url)
    model = model or DEFAULT_MODEL
    job_id = _find_existing(url, model, language) or submit_job(url, language, model)
    job = await _wait(job_id, wait_seconds, ctx)
    return _render(job, output, 0, max_chars)


@mcp.tool(annotations=_READS)
async def get_transcript(
    job_id: str,
    output: Output = "text",
    wait_seconds: int = 0,
    offset: int = 0,
    max_chars: int = 40000,
    ctx: Context | None = None,
) -> str:
    """Get the status or transcript of a job started by transcribe_url.

    Args:
        job_id: The id returned by transcribe_url or list_transcripts.
        output: "text", "timestamped" or "srt".
        wait_seconds: Wait up to this long (0-600) for an unfinished job.
        offset: Character offset, to page through long transcripts.
        max_chars: Maximum characters to return.
    """
    job = await _wait(job_id, wait_seconds, ctx)
    return _render(job, output, offset, max_chars)


@mcp.tool(annotations=_READS)
def list_transcripts(limit: int = 20) -> str:
    """List recent transcription jobs (newest first) with their ids, status, title and length."""
    rows = list_jobs(limit=max(1, min(limit, 200)))
    if not rows:
        return "No transcripts yet."
    return "\n".join(
        f"{j['id']} | {j['status']} | {_clock(j.get('duration'))} | {j.get('language') or '-'} | "
        f"{j.get('model')} | {j.get('title') or j['url']}" for j in rows)


# ---------------------------------------------------------------- transcript files
FileFormat = Literal["txt", "md", "srt", "vtt", "json"]
MIME = {"txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8",
        "srt": "application/x-subrip; charset=utf-8", "vtt": "text/vtt; charset=utf-8",
        "json": "application/json; charset=utf-8"}
INLINE_LIMIT = 80_000  # characters returned inside an MCP result; bigger files are link-only


def _paragraphs(segs: list[dict]) -> list[tuple[float, str]]:
    """Group Whisper's short segments into readable paragraphs (pause, or ~45-90 s of speech)."""
    paras, cur = [], []
    for s in segs:
        if not s["text"]:
            continue
        if cur:
            span = s["start"] - cur[0]["start"]
            if (s["start"] - cur[-1]["end"] >= 2.0 or span >= 90
                    or (span >= 45 and cur[-1]["text"].endswith((".", "?", "!", "।")))):
                paras.append(cur)
                cur = []
        cur.append(s)
    if cur:
        paras.append(cur)
    return [(p[0]["start"], " ".join(x["text"] for x in p)) for p in paras]


def _filename(job: dict, fmt: str) -> str:
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", job.get("title") or "")
    title = re.sub(r"\s+", " ", title).strip()[:80].rstrip(" .")
    return f"{title or 'transcript-' + job['id'][:8]}.{fmt}"


def build_file(job: dict, fmt: str) -> tuple[str, str]:
    """(filename, content) for a finished job."""
    segs = job.get("segments") or []
    if fmt == "json":
        return _filename(job, fmt), json.dumps(job, ensure_ascii=False, indent=2)
    if fmt == "srt":
        return _filename(job, fmt), "\n".join(
            f"{i}\n{_ts(s['start'], ',')} --> {_ts(s['end'], ',')}\n{s['text']}\n" for i, s in enumerate(segs, 1))
    if fmt == "vtt":
        return _filename(job, fmt), "WEBVTT\n\n" + "\n".join(
            f"{_ts(s['start'], '.')} --> {_ts(s['end'], '.')}\n{s['text']}\n" for s in segs)
    prob = job.get("language_probability")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.get("finished_at") or time.time()))
    meta = [("Title", job.get("title")), ("By", f"{job.get('uploader')} ({job.get('platform')})"),
            ("URL", job.get("url")), ("Length", _clock(job.get("duration"))),
            ("Language", f"{job.get('language')}" + (f" ({round(prob * 100)}% sure)" if prob else "")),
            ("Model", f"Whisper {job.get('model')}"), ("Transcribed", when)]
    paras = _paragraphs(segs) or [(0.0, "(No speech was detected in this audio.)")]
    if fmt == "md":
        head = f"# {job.get('title') or 'Transcript'}\n\n" + "\n".join(
            f"- **{k}:** {v}" for k, v in meta[1:]) + "\n\n## Transcript\n\n"
        return _filename(job, fmt), head + "\n\n".join(f"**[{_clock(t)}]** {txt}" for t, txt in paras) + "\n"
    head = "\n".join(f"{k + ':':<13}{v}" for k, v in meta) + "\n" + "-" * 60 + "\n\n"
    return _filename(job, fmt), head + "\n\n".join(f"[{_clock(t)}] {txt}" for t, txt in paras) + "\n"


def _sign(job_id: str, fmt: str, exp: int) -> str:
    return hmac.new(API_TOKEN.encode(), f"{job_id}:{fmt}:{exp}".encode(), hashlib.sha256).hexdigest()


def signed_file_url(base: str, job_id: str, fmt: str) -> str:
    exp = int(time.time()) + LINK_TTL
    return f"{base}/jobs/{job_id}/file?format={fmt}&exp={exp}&sig={_sign(job_id, fmt, exp)}"


def _disposition(name: str) -> str:
    fallback = name.encode("ascii", "ignore").decode().replace('"', "").strip() or "transcript"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


def _not_ready(job: dict) -> dict:
    pct = round(100 * (job.get("progress") or 0))
    pos = job.get("queue_position")
    where = f", position {pos} in the queue" if pos else ""
    return {"id": job["id"], "status": job["status"], "progress": job.get("progress"), "queue_position": pos,
            "title": job.get("title"),
            "message": f"Job is still running ({job['status']}, {pct}% done{where}). Try again shortly."}


@app.get("/jobs/{job_id}/file", responses={202: {"description": "Job still running"},
                                           409: {"description": "Job failed"}})
def transcript_file(job_id: str, format: FileFormat = "txt", exp: int | None = None, sig: str | None = None,
                    authorization: str | None = Header(default=None)):
    """Download the transcript as a file. Until the job is done this returns 202 with a progress message."""
    signed = bool(API_TOKEN and exp and sig and exp >= time.time()
                  and hmac.compare_digest(sig, _sign(job_id, format, exp)))
    if not signed:
        auth(authorization)
    job = get_job(job_id)
    if job["status"] == "error":
        return JSONResponse({"id": job_id, "status": "error", "error": job.get("error")}, status_code=409)
    if job["status"] != "done":
        return JSONResponse(_not_ready(job), status_code=202, headers={"Retry-After": "30"})
    name, body = build_file(job, format)
    return Response(body, media_type=MIME[format], headers={"Content-Disposition": _disposition(name)})


def _base_url(ctx: Context | None) -> str:
    """PUBLIC_BASE_URL if set (reverse proxy / tunnel), else the address the client used."""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    try:
        return str(ctx.request_context.request.base_url).rstrip("/")
    except Exception:  # noqa: BLE001 - not over HTTP, or no request object
        return "http://localhost:8000"


@mcp.tool(annotations=_READS)
async def get_transcript_file(job_id: str, format: FileFormat = "txt", wait_seconds: int = 0,
                              ctx: Context | None = None):
    """Get a finished transcript as a file, plus a download link valid for 24 h that needs no token.

    If the job is not finished yet, says so (with progress) instead.

    Args:
        job_id: The id returned by transcribe_url or list_transcripts.
        format: "txt" (readable: header + timestamped paragraphs, default), "md" (Markdown),
            "srt" / "vtt" (subtitles) or "json" (everything, incl. segment timings).
        wait_seconds: Wait up to this long (0-600) for an unfinished job.
    """
    job = await _wait(job_id, wait_seconds, ctx)
    if job["status"] == "error":
        return f"Transcription failed (job {job['id']}): {job.get('error')}"
    if job["status"] != "done":
        return (_not_ready(job)["message"] + f' Call get_transcript_file(job_id="{job["id"]}", '
                f'wait_seconds=300) to wait for it.')
    name, body = build_file(job, format)
    url = signed_file_url(_base_url(ctx), job["id"], format)
    note = (f"Transcript file ready: {name} ({max(1, round(len(body.encode()) / 1024))} KB).\n"
            f"Download link (valid {LINK_TTL // 3600} h, no token needed): {url}\n"
            f'For example: curl -o "{name}" "{url}"')
    if len(body) > INLINE_LIMIT:
        return note + (f"\nThe file has {len(body)} characters, too many to include here: download it, or read it "
                       "in pages with get_transcript(offset=...).")
    return [TextContent(type="text", text=note),
            EmbeddedResource(type="resource", resource=TextResourceContents(
                uri=f"transcript://{job['id']}/{quote(name)}", mime_type=MIME[format].split(";")[0], text=body))]


class BearerAuth:
    """ASGI guard for the MCP endpoint (FastAPI dependencies don't apply to raw ASGI routes)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and API_TOKEN:
            got = dict(scope.get("headers") or []).get(b"authorization", b"").decode(errors="replace")
            if not secrets.compare_digest(got, f"Bearer {API_TOKEN}"):
                await JSONResponse({"detail": "Missing or wrong token. Send 'Authorization: Bearer <API_TOKEN>'."},
                                   status_code=401)(scope, receive, send)
                return
        await self.inner(scope, receive, send)


# Creates mcp.session_manager (run in the lifespan above). Stateless: every request stands alone, so agents
# need no session handling. DNS-rebinding protection is off so LAN / Tailscale host names work; the bearer
# token (BearerAuth below) is the guard.
mcp.streamable_http_app(stateless_http=True,
                        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


async def _mcp_asgi(scope, receive, send):
    await mcp.session_manager.handle_request(scope, receive, send)


app.router.routes.append(Route("/mcp", endpoint=BearerAuth(_mcp_asgi)))
