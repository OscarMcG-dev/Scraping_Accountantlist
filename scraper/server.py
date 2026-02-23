"""
Minimal API to feed input files and pull output from the scraper on Railway.

- POST /upload: upload a CSV or TXT file (stored under DATA_DIR/input/)
- POST /run: run a script (enrich_justcall or main) with an optional input file
- GET /run/status: see if a run is in progress and tail of live logs (observability)
- GET /output: list output files
- GET /output/{filename}: download an output file

Set DATA_DIR in env (e.g. /data) when using a Railway volume; default is data/ for local.
Set SCRAPER_RUN_TIMEOUT_SECONDS to change max run time (default 3600 = 1 hour).
"""
import os
import subprocess
import threading
import uuid
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

app = FastAPI(
    title="Scraper API",
    description="Upload input files, trigger enrichments, download output CSVs.",
)


def ensure_dirs():
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


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
            "run": "POST /run — body: {\"script\": \"enrich_justcall\"|\"main\", \"input_file\": \"name.csv\", \"concurrency\": 4}",
            "run_status": "GET /run/status — running? + live log tail",
            "input_list": "GET /input — list uploaded files",
            "output_list": "GET /output — list output files",
            "output_download": "GET /output/{filename} — download file",
            "state_list": "GET /state — list state/checkpoint files",
            "state_download": "GET /state/{filename} — download state file",
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
        try:
            proc.wait(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            with _run_lock:
                _running = False
            raise HTTPException(
                status_code=504,
                detail=f"Run timed out after {RUN_TIMEOUT} seconds. Set SCRAPER_RUN_TIMEOUT_SECONDS for a different limit.",
            )
        _last_exit_code = proc.returncode
    finally:
        with _run_lock:
            _running = False

    if proc.returncode != 0:
        tail = ""
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Scraper exited with non-zero code. Check GET /run/status for full logs.",
                "exit_code": proc.returncode,
                "log_tail": tail,
            },
        )

    if script == "enrich_justcall":
        return {"output_file": out_name, "message": f"Download via GET /output/{out_name}"}
    return {
        "message": "Pipeline finished. List files with GET /output and download as needed.",
    }


@app.get("/run/status")
def run_status():
    """
    Observability: see if a run is in progress and get the tail of live logs.
    Poll this while POST /run is in progress to confirm the scraper isn't silently failing.
    """
    ensure_dirs()
    log_path = STATE_DIR / "run.log"
    log_tail = ""
    if log_path.exists():
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            log_tail = content[-8000:]  # last 8k chars
        except Exception:
            log_tail = "(could not read log)"
    with _run_lock:
        running = _running
        last_exit = _last_exit_code
    return {
        "running": running,
        "last_exit_code": last_exit,
        "log_tail": log_tail,
        "timeout_seconds": RUN_TIMEOUT,
    }


@app.get("/output")
def list_output():
    """List output files (CSVs) in DATA_DIR/output."""
    ensure_dirs()
    files = [f.name for f in OUTPUT_DIR.iterdir() if f.is_file()]
    return {"files": sorted(files)}


@app.get("/output/{filename}")
def download_output(filename: str):
    """Download an output file by name."""
    path = OUTPUT_DIR / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)


@app.get("/input")
def list_input():
    """List uploaded input files in DATA_DIR/input."""
    ensure_dirs()
    files = [f.name for f in INPUT_DIR.iterdir() if f.is_file()]
    return {"files": sorted(files)}


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
