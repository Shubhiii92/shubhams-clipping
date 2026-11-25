"""Loopback-only guest upload portal. Tunnel this port, never the studio API."""

import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
import re
import secrets
import shutil
import signal
import sys
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.requests import ClientDisconnect

ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent
JOBS = ROOT / "data" / "guest-jobs"
JOBS.mkdir(mode=0o700, parents=True, exist_ok=True)
MAX_BYTES = 40 * 1024 * 1024
RETENTION = 24 * 60 * 60
ID = re.compile(r"[a-f0-9]{48}")
FORMATS = {"reel": "instagram-reel", "short": "youtube-short"}
job_lock = asyncio.Lock()
workers: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(_app):
    yield
    tasks = tuple(workers.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def set_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


@app.get("/")
async def home():
    return FileResponse(ASSETS / "index.html")


@app.get("/guest.js")
async def javascript():
    return FileResponse(ASSETS / "guest.js", media_type="text/javascript")


@app.get("/style.css")
async def stylesheet():
    return FileResponse(ROOT / "docs" / "preview" / "style.css", media_type="text/css")


def set_state(job, status, **details):
    temp = job / "state.tmp"
    temp.write_text(json.dumps(dict(status=status, **details)))
    temp.replace(job / "state.json")


def reserve_slot():
    recent = 0
    for job in JOBS.iterdir():
        if not ID.fullmatch(job.name) or job.is_symlink() or not job.is_dir():
            continue
        marker = job / "submitted"
        if time.time() - (marker if marker.exists() else job).stat().st_mtime >= RETENTION:
            shutil.rmtree(job)
        else:
            recent += 1
    if recent >= 12:
        raise HTTPException(429, "The guest studio has reached its daily limit.")
    if shutil.disk_usage(JOBS).free < 2 * 1024**3:
        raise HTTPException(503, "The guest studio is low on disk space.")


async def run_command(args, *, env=None, timeout=45, output=True):
    target = asyncio.subprocess.PIPE if output else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=ROOT, env=env, stdout=target, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    if proc.returncode:
        raise ValueError("Video processing failed")
    return data


async def process(job, profile):
    raw = job / "upload.mp4"
    downloads = job / "downloads"
    downloads.mkdir()
    source = downloads / "guest_source.mp4"
    try:
        probe = await run_command([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mov",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json", str(raw),
        ], timeout=12)
        info = json.loads(probe)
        duration = float(info.get("format", {}).get("duration", 0))
        video = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
        width, height = video.get("width", 0), video.get("height", 0)
        if not (math.isfinite(duration) and 10 <= duration <= 90
                and 0 < width <= 3840 and 0 < height <= 3840
                and any(s["codec_type"] == "audio" for s in info["streams"])):
            set_state(job, "error", message="Use a 10–90 second MP4 with video and audio.")
            return

        # Decode only the selected tracks and hand the studio a fresh local file.
        await run_command([
            "ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-f", "mov", "-i", str(raw), "-t", "90", "-map", "0:v:0", "-map", "0:a:0",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-vf", "scale=1080:1080:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30",
            "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            "-c:a", "aac", "-ac", "2", "-movflags", "+faststart", str(source),
        ], timeout=100, output=False)
        raw.unlink()
        config = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
        config.update(model="gemma3:1b", channel="", channels=[], auto_upload=False)
        config["whisper"].update(model="base", device="cpu")
        config["llm"].update(num_ctx=4096, temperature=0.2)
        config["clips"].update(vertical=True, captions=True, max_clips_per_video=1, min_score=0)
        config["video"].update(encoder="auto", parallel_renders=1)
        config_path = job / "settings.yaml"
        config_path.write_text(yaml.safe_dump(config))
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "HF_HOME") if key in os.environ}
        env.update(CLIPS_STUDIO_DATA_DIR=str(job),
                   CLIPS_STUDIO_OLLAMA_HOST="http://127.0.0.1:11434")
        await run_command([
            sys.executable, "main.py", "--config", str(config_path), "process", "local:guest_source"
        ], env=env, timeout=300, output=False)
        clip = next((job / "clips").rglob("*.mp4"), None)
        if clip is None:
            set_state(job, "error", message="No highlight was found. Try a clip with clearer speech.")
            return
        clip.replace(job / "result.mp4")
        set_state(job, "done", filename=f"shubhams-clipping-{FORMATS[profile]}.mp4")
    except (TimeoutError, ValueError, OSError, KeyError):
        set_state(job, "error", message="Couldn’t process this video. Try a short MP4 with clear speech.")
    finally:
        try:
            raw.unlink(missing_ok=True)
            for directory in ("downloads", "clips", "transcripts"):
                shutil.rmtree(job / directory, ignore_errors=True)
            for file in job.glob("state.db*"):
                file.unlink(missing_ok=True)
        finally:
            job_lock.release()


@app.post("/jobs", status_code=202)
async def upload(request: Request):
    profile = request.headers.get("X-Clip-Format")
    if profile not in FORMATS or request.headers.get("content-type") != "video/mp4":
        raise HTTPException(415, "Choose a format and an MP4 file.")
    length = request.headers.get("content-length", "")
    if not re.fullmatch(r"[0-9]{1,9}", length) or not 0 < int(length) <= MAX_BYTES:
        raise HTTPException(413, "The MP4 must be under 40 MB.")
    if job_lock.locked():
        raise HTTPException(429, "The Mac is processing another clip. Try again shortly.")
    await job_lock.acquire()
    job = None
    handed_off = False
    try:
        reserve_slot()
        job = JOBS / secrets.token_hex(24)
        job.mkdir(mode=0o700)
        size = 0
        raw = job / "upload.mp4"
        async with asyncio.timeout(35):
            with raw.open("wb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > int(length):
                        raise HTTPException(413, "The upload exceeds the stated size.")
                    handle.write(chunk)
        with raw.open("rb") as handle:
            signature = handle.read(12)
        if size != int(length) or signature[4:8] != b"ftyp":
            raise HTTPException(415, "This file isn’t a complete MP4.")
        (job / "submitted").touch()
        set_state(job, "processing")
        worker = asyncio.create_task(process(job, profile))
        workers[job.name] = worker
        worker.add_done_callback(lambda _: workers.pop(job.name, None))
        handed_off = True
        return {"id": job.name}
    except (TimeoutError, ClientDisconnect):
        raise HTTPException(408, "The upload was interrupted. Try again.")
    finally:
        if not handed_off:
            if job is not None:
                shutil.rmtree(job, ignore_errors=True)
            job_lock.release()


def get_job(token):
    job = JOBS / token
    marker = job / "submitted"
    if not ID.fullmatch(token) or not marker.is_file() or time.time() - marker.stat().st_mtime >= RETENTION:
        raise HTTPException(404, "That clip link has expired or is invalid.")
    state = json.loads((job / "state.json").read_text())
    if state["status"] == "processing" and token not in workers:
        state = {"status": "error", "message": "The studio restarted. Upload the video again."}
    return job, state


@app.get("/jobs/{token}")
async def job_status(token: str):
    _, state = get_job(token)
    return state


@app.get("/jobs/{token}/download")
async def download(token: str):
    job, state = get_job(token)
    if state["status"] != "done":
        raise HTTPException(404, "This clip isn’t ready.")
    return FileResponse(job / "result.mp4", media_type="video/mp4", filename=state["filename"])
