"""Tests that need no network, no model and no ffmpeg: formatting, auth, URL guard, files, MCP tools."""
import asyncio
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import app as srv

AUTH = {"Authorization": "Bearer test-token"}

SEGMENTS = [
    {"start": 0.0, "end": 3.5, "text": "Hello and welcome."},
    {"start": 3.6, "end": 7.0, "text": "Today we talk about Whisper."},
    {"start": 10.0, "end": 12.0, "text": "After a pause, a new paragraph."},
]


def done_job(**over) -> dict:
    job = {"id": str(uuid.uuid4()), "url": "https://www.youtube.com/watch?v=abc", "model": "large-v3-turbo",
           "language": "en", "language_probability": 0.99, "status": "done", "progress": 1.0,
           "title": 'My "Talk": part 1/2', "uploader": "Someone", "platform": "Youtube", "duration": 12.0,
           "created_at": time.time(), "finished_at": time.time(), "segments": SEGMENTS,
           "text": " ".join(s["text"] for s in SEGMENTS)}
    job.update(over)
    srv._save(job)
    return job


@pytest.fixture(scope="session")
def client():
    with TestClient(srv.app) as c:  # runs the lifespan (worker thread + MCP session manager) once
        yield c


# ------------------------------------------------------------- formatting
def test_timestamps():
    assert srv._ts(3723.456, ",") == "01:02:03,456"
    assert srv._clock(65) == "1:05"
    assert srv._clock(3723) == "1:02:03"


def test_paragraphs_split_on_pause():
    paras = srv._paragraphs(SEGMENTS)
    assert [round(t) for t, _ in paras] == [0, 10]
    assert paras[0][1] == "Hello and welcome. Today we talk about Whisper."


def test_filename_is_safe():
    assert srv._filename({"id": "x", "title": 'My "Talk": part 1/2'}, "txt") == "My Talk part 1 2.txt"
    assert srv._filename({"id": "12345678-aaaa", "title": None}, "md") == "transcript-12345678.md"
    assert len(srv._filename({"id": "x", "title": "a" * 300}, "txt")) <= 84


@pytest.mark.parametrize("fmt,needle", [
    ("txt", "[0:10] After a pause"), ("md", "**[0:00]** Hello"), ("srt", "00:00:03,600 --> 00:00:07,000"),
    ("vtt", "WEBVTT"), ("json", '"segments"')])
def test_build_file(fmt, needle):
    name, body = srv.build_file(done_job(), fmt)
    assert name.endswith("." + fmt)
    assert needle in body


def test_empty_transcript_says_so():
    _, body = srv.build_file(done_job(segments=[], text=""), "txt")
    assert "No speech was detected" in body


# ------------------------------------------------------------- URL guard
@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://localhost:8000/", "http://192.168.1.10/v.mp4",
                                 "http://169.254.169.254/latest/meta-data", "http://[::1]/", "ftp://example.com/a"])
def test_private_and_odd_urls_are_refused(url):
    with pytest.raises(ValueError):
        srv.check_url(url)


def test_public_ip_is_allowed():
    srv.check_url("https://8.8.8.8/video")


# ------------------------------------------------------------- REST
def test_health_needs_no_token(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_token_required(client):
    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/jobs", headers=AUTH).status_code == 200


def test_transcribe_refuses_private_url(client):
    r = client.post("/transcribe", json={"url": "http://127.0.0.1:8000/health"}, headers=AUTH)
    assert r.status_code == 400 and "private" in r.json()["detail"]


def test_file_download_and_signed_link(client):
    job = done_job()
    r = client.get(f"/jobs/{job['id']}/file?format=md", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"]

    path = srv.signed_file_url("", job["id"], "txt")
    assert client.get(path).status_code == 200                           # no token, valid signature
    assert client.get(path.replace("sig=", "sig=0")).status_code == 401  # tampered
    assert client.get(path.replace("format=txt", "format=md")).status_code == 401  # other format


def test_file_while_running_returns_202(client):
    job_id = str(uuid.uuid4())
    with srv._lock:
        srv._active[job_id] = {"id": job_id, "url": "https://example.com/v", "status": "transcribing",
                               "progress": 0.45, "created_at": time.time(), "model": "large-v3-turbo"}
    try:
        r = client.get(f"/jobs/{job_id}/file", headers=AUTH)
        assert r.status_code == 202
        assert r.headers["retry-after"] == "30"
        assert "45% done" in r.json()["message"]
    finally:
        with srv._lock:
            srv._active.pop(job_id)


def test_mcp_endpoint_needs_token(client):
    assert client.post("/mcp", json={}).status_code == 401


# ------------------------------------------------------------- MCP tools
def test_tools_are_listed_with_annotations():
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    assert set(tools) == {"transcribe_url", "get_transcript", "list_transcripts", "get_transcript_file"}
    assert tools["get_transcript"].annotations.read_only_hint is True
    assert tools["transcribe_url"].annotations.destructive_hint is False


def _call(name, args):
    result = asyncio.run(srv.mcp.call_tool(name, args))
    assert not result.is_error, result.content
    return result.content


def test_get_transcript_and_file_tools():
    job = done_job()
    text = _call("get_transcript", {"job_id": job["id"], "output": "timestamped"})[0].text
    assert "[0:03] Today we talk about Whisper." in text

    blocks = _call("get_transcript_file", {"job_id": job["id"], "format": "txt"})
    assert "Download link" in blocks[0].text and "sig=" in blocks[0].text
    assert "Hello and welcome." in blocks[1].resource.text


def test_transcribe_url_reuses_finished_job():
    job = done_job(url="https://www.youtube.com/watch?v=reuse-me")
    text = _call("transcribe_url", {"url": job["url"], "wait_seconds": 0})[0].text
    assert f"Job id: {job['id']}" in text


def test_list_transcripts_tool():
    done_job(title="Listed video")
    assert "Listed video" in _call("list_transcripts", {"limit": 50})[0].text


def test_jobs_are_json_on_disk():
    job = done_job()
    assert json.loads(srv._path(job["id"]).read_text())["id"] == job["id"]


def test_server_identity():
    assert asyncio.run(srv.mcp.list_tools())  # tools register on the 2.x MCPServer
    assert srv.mcp.name == "social-whisper-transcript"
