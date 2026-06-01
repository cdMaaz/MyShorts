"""
app.py
FastAPI backend for MyShorts.

Endpoints:
  POST /api/jobs              — Start a new job (URL or file upload + options)
  GET  /api/jobs/{id}         — Poll job status + incremental clip results
  GET  /api/jobs/{id}/stream  — SSE log stream
  DELETE /api/jobs/{id}       — Cancel / cleanup job
  GET  /clips/{id}/{filename} — Serve processed video clips

Clips are served from OUTPUT_DIR/<job_id>/<filename>.
Results are read from OUTPUT_DIR/<job_id>/results.json (written by pipeline.py).
"""

import asyncio
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Dict, Optional

import aiofiles
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_JOBS", "3"))
JOB_TTL = int(os.environ.get("JOB_TTL_SECONDS", "7200"))  # 2 hours

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── State ──────────────────────────────────────────────────────────────────────
# jobs[job_id] = {
#   status: queued | processing | completed | failed | cancelled
#   logs: [str, ...]
#   process: subprocess.Popen | None
#   created_at: float
#   cmd: [...]
#   env: {}
#   output_dir: str
# }
jobs: Dict[str, dict] = {}
_semaphore: asyncio.Semaphore = None  # initialised in lifespan
_job_queue: asyncio.Queue = None


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _semaphore, _job_queue
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    _job_queue = asyncio.Queue()

    workers = [
        asyncio.create_task(_queue_worker()),
        asyncio.create_task(_cleanup_loop()),
    ]
    yield
    for w in workers:
        w.cancel()


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="MyShorts API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve clip files at /clips/<job_id>/<filename>
app.mount("/clips", StaticFiles(directory=OUTPUT_DIR), name="clips")


# ── Queue worker ───────────────────────────────────────────────────────────────

async def _queue_worker():
    while True:
        try:
            job_id = await _job_queue.get()
            await _semaphore.acquire()
            asyncio.create_task(_run_job(job_id))
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[queue] error: {e}", flush=True)
            await asyncio.sleep(1)


async def _run_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        _semaphore.release()
        return

    job["status"] = "processing"
    cmd = job["cmd"]
    env = job["env"]

    def _read_stdout(proc, jid):
        """Thread: read subprocess stdout and append to logs."""
        try:
            for raw_line in iter(proc.stdout.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if jid in jobs:
                    jobs[jid]["logs"].append(line)
                print(f"[job:{jid[:8]}] {line}", flush=True)
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        job["process"] = proc

        t = threading.Thread(target=_read_stdout, args=(proc, job_id), daemon=True)
        t.start()

        # Async wait
        while proc.poll() is None:
            await asyncio.sleep(1.5)

        t.join(timeout=5)
        rc = proc.returncode

        if rc == 0:
            job["status"] = "completed"
        else:
            if job["status"] != "cancelled":
                job["status"] = "failed"

    except Exception as e:
        job["status"] = "failed"
        job["logs"].append(f"Runner error: {e}")
    finally:
        _semaphore.release()
        _job_queue.task_done()


# ── Cleanup ────────────────────────────────────────────────────────────────────

async def _cleanup_loop():
    while True:
        try:
            await asyncio.sleep(300)  # every 5 min
            now = time.time()
            for jid in list(jobs.keys()):
                j = jobs[jid]
                if now - j.get("created_at", now) > JOB_TTL:
                    _kill_job(jid)
                    out_dir = j.get("output_dir", "")
                    if out_dir and os.path.isdir(out_dir):
                        shutil.rmtree(out_dir, ignore_errors=True)
                    upload_path = j.get("upload_path", "")
                    if upload_path and os.path.exists(upload_path):
                        os.remove(upload_path)
                    del jobs[jid]
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[cleanup] {e}", flush=True)


def _kill_job(job_id: str):
    j = jobs.get(job_id)
    if j and j.get("process"):
        try:
            j["process"].terminate()
        except Exception:
            pass


# ── Helper: read results.json ──────────────────────────────────────────────────

def _read_results(job_id: str) -> Optional[dict]:
    out_dir = jobs.get(job_id, {}).get("output_dir", "")
    if not out_dir:
        return None
    rp = os.path.join(out_dir, "results.json")
    if not os.path.exists(rp):
        return None
    try:
        with open(rp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/api/jobs")
async def create_job(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    hooks: bool = Form(True),
    zoom: bool = Form(True),
    subtitles: bool = Form(True),
):
    """Create and queue a new processing job."""

    # API key — accept from header OR form
    gemini_key = (
        request.headers.get("X-Gemini-Key")
        or request.headers.get("x-gemini-key")
    )

    # Also accept JSON body for URL-only requests
    content_type = request.headers.get("content-type", "")
    if not url and not file:
        if "application/json" in content_type:
            body = await request.json()
            url = body.get("url")
            hooks = body.get("hooks", True)
            zoom = body.get("zoom", True)
            subtitles = body.get("subtitles", True)
            if not gemini_key:
                gemini_key = body.get("gemini_key")

    if not url and not file:
        raise HTTPException(400, "Provide a YouTube URL or upload a video file.")

    if not gemini_key:
        raise HTTPException(400, "Gemini API key required (X-Gemini-Key header).")

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = gemini_key

    # Build pipeline command
    cmd = [
        sys.executable, "-u",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline.py"),
        "--job-id", job_id,
        "--output-dir", os.path.abspath(job_dir),
    ]

    if hooks:
        cmd.append("--hooks")
    else:
        cmd.append("--no-hooks")

    if zoom:
        cmd.append("--zoom")
    else:
        cmd.append("--no-zoom")

    if subtitles:
        cmd.append("--subtitles")
    else:
        cmd.append("--no-subtitles")

    upload_path = None

    if url:
        cmd += ["--url", url]
    else:
        # Save uploaded file
        upload_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file.filename}")
        async with aiofiles.open(upload_path, "wb") as f_out:
            content = await file.read()
            await f_out.write(content)
        cmd += ["--input", os.path.abspath(upload_path)]

    jobs[job_id] = {
        "status": "queued",
        "logs": [f"Job {job_id} queued."],
        "process": None,
        "created_at": time.time(),
        "cmd": cmd,
        "env": env,
        "output_dir": os.path.abspath(job_dir),
        "upload_path": upload_path,
    }

    await _job_queue.put(job_id)

    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    """Poll job status. Returns clips incrementally as they are processed."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found.")

    j = jobs[job_id]
    results = _read_results(job_id)

    clips = []
    error = None
    stage = None

    if results:
        clips = results.get("clips", [])
        error = results.get("error")
        stage = results.get("stage")

    # Determine final status
    status = j["status"]
    # Sync with results.json in case process finished but we haven't updated yet
    if results:
        r_status = results.get("status")
        if r_status in ("completed", "failed") and status not in ("cancelled",):
            status = r_status
            j["status"] = status

    return {
        "job_id": job_id,
        "status": status,
        "stage": stage,
        "clips": clips,
        "error": error,
        "log_count": len(j["logs"]),
    }


@app.get("/api/jobs/{job_id}/logs")
async def stream_logs(job_id: str, after: int = 0):
    """
    Server-Sent Events stream of job logs.
    ?after=N to only receive logs after line N (for reconnection).
    """
    if job_id not in jobs:
        raise HTTPException(404, "Job not found.")

    async def _generate():
        idx = after
        while True:
            j = jobs.get(job_id)
            if not j:
                yield "data: [job expired]\n\n"
                break

            logs = j["logs"]
            while idx < len(logs):
                line = logs[idx]
                yield f"data: {json.dumps({'line': line, 'i': idx})}\n\n"
                idx += 1

            # Stop if job finished
            if j["status"] in ("completed", "failed", "cancelled"):
                yield f"data: {json.dumps({'done': True, 'status': j['status']})}\n\n"
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(_generate(), media_type="text/event-stream")


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a running job and clean up its files."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found.")

    j = jobs[job_id]
    _kill_job(job_id)
    j["status"] = "cancelled"

    return {"job_id": job_id, "status": "cancelled"}


@app.get("/api/health")
async def health():
    active = sum(1 for j in jobs.values() if j["status"] in ("queued", "processing"))
    return {"ok": True, "active_jobs": active, "total_jobs": len(jobs)}


# ── Dev: serve frontend build ──────────────────────────────────────────────────
# In production (Docker), the built React app is served from /app/frontend/dist
FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isdir(FRONTEND_DIST):
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles as _SF

    app.mount("/assets", _SF(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")

    @app.get("/")
    @app.get("/{_:path}")
    async def serve_spa(_: str = ""):
        return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
