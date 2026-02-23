"""
Minimal API to feed input files and pull output from the scraper on Railway.

- POST /upload: upload a CSV or TXT file (stored under DATA_DIR/input/)
- POST /run: run a script (enrich_justcall or main) with an optional input file
- GET /run/status: running?, log tail, uptime, progress, last run
- POST /run/cancel: cancel the current run
- GET /config: effective config (timeout, data_dir)
- GET /output, GET /input: list files with mtime

Set DATA_DIR in env (e.g. /data) when using a Railway volume; default is data/ for local.
Set SCRAPER_RUN_TIMEOUT_SECONDS to change max run time (default 3600 = 1 hour).
"""
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

# Use volume path on Railway if set; otherwise local data/
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
STATE_DIR = DATA_DIR / "state"

# Max time a single run is allowed (seconds). Env: SCRAPER_RUN_TIMEOUT_SECONDS (default 1 hour).
RUN_TIMEOUT = int(os.environ.get("SCRAPER_RUN_TIMEOUT_SECONDS", "3600"))

# Observability: live log file and running state (shared across requests)
RUN_LOG_FILE = STATE_DIR / "run.log"
_run_lock = threading.Lock()
_running = False
_last_exit_code: int | None = None
_current_proc: subprocess.Popen | None = None
_start_time = time.time()
LAST_RUN_FILE = STATE_DIR / "last_run.json"

app = FastAPI(
    title="Scraper API",
    description="Upload input files, trigger enrichments, download output CSVs.",
)


def ensure_dirs():
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _progress_from_checkpoint() -> dict | None:
    """Read checkpoint JSON(s) and return enriched/done counts if present."""
    for name in ("justcall_checkpoint.json", "checkpoint.json"):
        path = STATE_DIR / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            phase2 = data.get("phase2", {})
            enriched = phase2.get("enriched_urls", []) or list(phase2.get("enrichments", {}).keys())
            if enriched is not None:
                return {"checkpoint_file": name, "enriched_count": len(enriched) if isinstance(enriched, list) else 0}
        except Exception:
            pass
    return None


def _dashboard_html() -> str:
    path = Path(__file__).resolve().parent / "static" / "dashboard.html"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return "<!DOCTYPE html><html><body><p>Dashboard not found.</p><a href='/'>API</a></body></html>"


@app.get("/")
def root():
    ensure_dirs()
    return {
        "message": "Scraper API",
        "dashboard": "GET /dashboard — shareable web UI (upload, run, logs, download)",
        "endpoints": {
            "upload": "POST /upload — upload CSV/TXT",
            "run": "POST /run — body: {\"script\", \"input_file\"?, \"concurrency\"?}",
            "run_status": "GET /run/status — running?, log tail, uptime, progress, last_run",
            "run_cancel": "POST /run/cancel — cancel current run",
            "config": "GET /config — data_dir, timeout_seconds (read-only)",
            "input_list": "GET /input — list uploaded files (with mtime)",
            "output_list": "GET /output — list output files (with mtime)",
            "state_list": "GET /state — list state/checkpoint files",
            "output_download": "GET /output/{filename}",
            "state_download": "GET /state/{filename}",
        },
        "timeout_seconds": RUN_TIMEOUT,
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Shareable web UI: upload files, start runs with config, watch logs, download output/state."""
    return HTMLResponse(content=_dashboard_html())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """Upload an input file (e.g. Attio export CSV or URLs TXT). Stored under DATA_DIR/input/."""
    ensure_dirs()
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    safe_name = Path(file.filename).name
    path = INPUT_DIR / safe_name
    try:
        content = await file.read()
        path.write_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"filename": safe_name, "path": str(path)}


class RunRequest(BaseModel):
    script: str  # "enrich_justcall" | "main"
    input_file: str | None = None  # required for enrich_justcall
    concurrency: int | None = None  # optional; e.g. 4 for enrich_justcall / main


@app.post("/run")
async def run(body: RunRequest):
    """
    Run a scraper script.
    - script: "enrich_justcall" | "main"
    - input_file: required for enrich_justcall (filename under input/); ignored for main.
    """
    script = body.script
    input_file = body.input_file
    concurrency = body.concurrency
    ensure_dirs()
    if script not in ("enrich_justcall", "main"):
        raise HTTPException(
            status_code=400,
            detail="script must be 'enrich_justcall' or 'main'",
        )

    if script == "enrich_justcall":
        if not input_file:
            raise HTTPException(
                status_code=400,
                detail="input_file required for enrich_justcall (e.g. attio_export.csv)",
            )
        input_path = INPUT_DIR / Path(input_file).name
        if not input_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Input file not found: {input_file}. Upload it first via POST /upload.",
            )
        out_name = f"attio_people_enriched_{uuid.uuid4().hex[:8]}.csv"
        output_path = OUTPUT_DIR / out_name
        checkpoint_path = STATE_DIR / "justcall_checkpoint.json"
        cmd = [
            "python", "-m", "enrich_justcall",
            "--input", str(input_path),
            "--output", str(output_path),
            "--checkpoint", str(checkpoint_path),
        ]
        if concurrency is not None:
            cmd.extend(["--concurrency", str(concurrency)])
    else:
        # main.py: full directory pipeline (phase 1–3)
        checkpoint_path = STATE_DIR / "checkpoint.json"
        cmd = [
            "python", "-m", "main",
            "--checkpoint", str(checkpoint_path),
        ]
        output_path = OUTPUT_DIR  # main.py writes companies.csv / people.csv there

    env = os.environ.copy()
    env["OUTPUT_DIR"] = str(OUTPUT_DIR)
    if concurrency is not None:
        env["MAX_CONCURRENT_CRAWLS"] = str(concurrency)
        env["DIRECTORY_MAX_CONCURRENT"] = str(concurrency)

    global _running, _last_exit_code
    cwd = Path(__file__).resolve().parent
    log_path = STATE_DIR / "run.log"

    with _run_lock:
        if _running:
            raise HTTPException(status_code=409, detail="A run is already in progress. Check GET /run/status.")
        _running = True
        _last_exit_code = None

    def run_in_background():
        global _running, _last_exit_code, _current_proc
        proc = None
        try:
            with open(log_path, "w") as logf:
                proc = subprocess.Popen(
                    cmd,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=cwd,
                    env=env,
                )
            with _run_lock:
                _current_proc = proc
            try:
                proc.wait(timeout=RUN_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            finally:
                with _run_lock:
                    _last_exit_code = proc.returncode
                    _running = False
                    _current_proc = None
                ensure_dirs()
                try:
                    LAST_RUN_FILE.write_text(
                        json.dumps({
                            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "exit_code": proc.returncode,
                        }),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
        except Exception:
            with _run_lock:
                _running = False
                _last_exit_code = -1
                _current_proc = None

    thread = threading.Thread(target=run_in_background, daemon=True)
    thread.start()

    # Return immediately so dashboard and /run/status stay responsive
    if script == "enrich_justcall":
        return {
            "message": "Run started in background. Poll GET /run/status for progress and log tail. When done, list GET /output and download the new file.",
            "output_file_when_done": out_name,
        }
    return {
        "message": "Run started in background. Poll GET /run/status for progress. When done, list GET /output for companies.csv / people.csv.",
    }


@app.get("/run/status")
def run_status():
    """
    Observability: running?, log tail, uptime, progress from checkpoint, last run info.
    """
    ensure_dirs()
    log_path = STATE_DIR / "run.log"
    log_tail = ""
    if log_path.exists():
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            log_tail = content[-8000:]
        except Exception:
            log_tail = "(could not read log)"
    with _run_lock:
        running = _running
        last_exit = _last_exit_code
    last_run = None
    if LAST_RUN_FILE.exists():
        try:
            last_run = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "running": running,
        "last_exit_code": last_exit,
        "log_tail": log_tail,
        "timeout_seconds": RUN_TIMEOUT,
        "uptime_seconds": round(time.time() - _start_time, 1),
        "progress": _progress_from_checkpoint(),
        "last_run": last_run,
    }


@app.post("/run/cancel")
def run_cancel():
    """Cancel the current run (if any). Returns 200 when a run was canceled or 404 if none was running."""
    global _current_proc
    with _run_lock:
        if not _running or _current_proc is None:
            raise HTTPException(status_code=404, detail="No run in progress")
        proc = _current_proc
    try:
        proc.kill()
    except Exception:
        pass
    return {"message": "Cancel requested. Run should stop shortly."}


@app.get("/config")
def get_config():
    """Effective config (from env) for visibility. Read-only."""
    return {
        "data_dir": str(DATA_DIR),
        "timeout_seconds": RUN_TIMEOUT,
        "input_dir": str(INPUT_DIR),
        "output_dir": str(OUTPUT_DIR),
        "state_dir": str(STATE_DIR),
    }


def _file_list_with_mtime(dir_path: Path) -> list[dict]:
    files = []
    for f in dir_path.iterdir():
        if f.is_file():
            try:
                mtime = f.stat().st_mtime
                mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                mtime_iso = None
            files.append({"name": f.name, "mtime_iso": mtime_iso})
    return sorted(files, key=lambda x: x["name"])


@app.get("/output")
def list_output():
    """List output files (CSVs) in DATA_DIR/output with last modified time."""
    ensure_dirs()
    return {"files": _file_list_with_mtime(OUTPUT_DIR)}


@app.get("/output/{filename}")
def download_output(filename: str):
    """Download an output file by name."""
    path = OUTPUT_DIR / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)


@app.get("/input")
def list_input():
    """List uploaded input files in DATA_DIR/input with last modified time."""
    ensure_dirs()
    return {"files": _file_list_with_mtime(INPUT_DIR)}


@app.get("/input/{filename}")
def download_input(filename: str):
    """Download an input file by name."""
    path = INPUT_DIR / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)


@app.get("/state")
def list_state():
    """List state/checkpoint files in DATA_DIR/state."""
    ensure_dirs()
    files = [f.name for f in STATE_DIR.iterdir() if f.is_file()]
    return {"files": sorted(files)}


@app.get("/state/{filename}")
def download_state(filename: str):
    """Download a state file by name (e.g. checkpoint JSON, run.log)."""
    path = STATE_DIR / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)
