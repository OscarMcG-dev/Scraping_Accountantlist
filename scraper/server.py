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
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from config import Settings
from website_enricher import get_default_crawl_prompts

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
    """Read checkpoint JSON(s) and return enriched/done counts with rate estimates."""
    for name in ("justcall_checkpoint.json", "url_enrichment_checkpoint.json", "checkpoint.json"):
        path = STATE_DIR / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            phase2 = data.get("phase2", {})
            enriched = phase2.get("enriched_urls", []) or list(phase2.get("enrichments", {}).keys())
            if enriched is not None:
                enriched_count = len(enriched) if isinstance(enriched, list) else 0
                enrichments_with_data = len(phase2.get("enrichments", {}))
                result = {
                    "checkpoint_file": name,
                    "enriched_count": enriched_count,
                    "enrichments_with_data": enrichments_with_data,
                }
                return result
        except Exception:
            pass
    return None


def _parse_progress_from_log() -> dict | None:
    """Parse the latest [N/M] progress line and rate/ETA from the run log."""
    log_path = STATE_DIR / "run.log"
    if not log_path.is_file():
        return None
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        for line in reversed(lines):
            if "/min, ETA" in line and "[" in line:
                m = re.search(r"\[(\d+)/(\d+)\].*\[([0-9.]+)/min, ETA (\d+)min\]", line)
                if m:
                    return {
                        "current": int(m.group(1)),
                        "total": int(m.group(2)),
                        "rate_per_min": float(m.group(3)),
                        "eta_min": int(m.group(4)),
                        "percent": round(int(m.group(1)) / int(m.group(2)) * 100, 1) if int(m.group(2)) else 0,
                    }
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
            "run": "POST /run — body: {\"script\", \"input_file\"?, \"output_format\"?, \"concurrency\"?} (script: enrich_justcall | enrich_urls | main)",
            "run_status": "GET /run/status — running?, log tail, uptime, progress, last_run",
            "run_cancel": "POST /run/cancel — cancel current run",
            "config": "GET /config — data_dir, timeout_seconds (read-only)",
            "input_list": "GET /input — list uploaded files (with mtime, size)",
            "output_list": "GET /output — list output files (with mtime, size)",
            "output_download": "GET /output/{filename}",
            "output_delete": "DELETE /output/{filename}",
            "state_list": "GET /state — list state/checkpoint files (with mtime, size)",
            "state_download": "GET /state/{filename}",
            "state_delete": "DELETE /state/{filename} — delete checkpoint to reset a run",
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
    script: str  # "enrich_justcall" | "main" | "enrich_urls"
    input_file: str | None = None  # required for enrich_justcall and enrich_urls
    output_format: str | None = None  # for enrich_urls: "default" | "justcall"
    concurrency: int | None = None  # optional; e.g. 4 for enrich_justcall / enrich_urls / main


@app.post("/run")
async def run(body: RunRequest):
    """
    Run a scraper script.
    - script: "enrich_justcall" | "main" | "enrich_urls"
    - input_file: required for enrich_justcall and enrich_urls (filename under input/); ignored for main.
    - output_format: for enrich_urls only — "default" (companies + people) or "justcall" (single Attio People CSV).
    """
    script = body.script
    input_file = body.input_file
    output_format = body.output_format
    concurrency = body.concurrency
    ensure_dirs()
    if script not in ("enrich_justcall", "main", "enrich_urls"):
        raise HTTPException(
            status_code=400,
            detail="script must be 'enrich_justcall', 'main', or 'enrich_urls'",
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
    elif script == "enrich_urls":
        if not input_file:
            raise HTTPException(
                status_code=400,
                detail="input_file required for enrich_urls (e.g. urls.txt or firms.csv). Upload via POST /upload.",
            )
        input_path = INPUT_DIR / Path(input_file).name
        if not input_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Input file not found: {input_file}. Upload it first via POST /upload.",
            )
        of = (output_format or "justcall").lower()
        if of not in ("default", "justcall"):
            of = "justcall"
        checkpoint_path = STATE_DIR / "url_enrichment_checkpoint.json"
        cmd = [
            "python", "-m", "enrich_urls",
            "--input", str(input_path),
            "--output", str(OUTPUT_DIR),
            "--checkpoint", str(checkpoint_path),
            "--output-format", of,
        ]
        out_name = None
        if of == "justcall":
            out_name = f"url_enrichment_people_{uuid.uuid4().hex[:8]}.csv"
            cmd.extend(["--justcall-output", str(OUTPUT_DIR / out_name)])
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
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            with open(log_path, "w") as logf:
                logf.write(f"# Script: {script} | Started: {started_at}\n")
                logf.write(f"# Command: {' '.join(cmd)}\n")
                logf.flush()
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
                    logf.write(f"\n# TIMEOUT: Run exceeded {RUN_TIMEOUT}s limit, killing process.\n")
                    logf.flush()
                    proc.kill()
                    proc.wait()
            with _run_lock:
                _last_exit_code = proc.returncode
                _running = False
                _current_proc = None
            ensure_dirs()
            try:
                LAST_RUN_FILE.write_text(
                    json.dumps({
                        "script": script,
                        "started_at": started_at,
                        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "exit_code": proc.returncode,
                    }),
                    encoding="utf-8",
                )
            except Exception:
                pass
        except Exception as exc:
            with _run_lock:
                _running = False
                _last_exit_code = -1
                _current_proc = None
            try:
                with open(log_path, "a") as logf:
                    logf.write(f"\n# INTERNAL ERROR: {exc}\n")
            except Exception:
                pass

    thread = threading.Thread(target=run_in_background, daemon=True)
    thread.start()

    # Return immediately so dashboard and /run/status stay responsive
    if script == "enrich_justcall":
        return {
            "message": "Run started in background. Poll GET /run/status for progress and log tail. When done, list GET /output and download the new file.",
            "output_file_when_done": out_name,
        }
    if script == "enrich_urls":
        return {
            "message": "Run started in background. Poll GET /run/status for progress. When done, list GET /output and download the output file(s).",
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
    progress = _progress_from_checkpoint()
    log_progress = _parse_progress_from_log() if running else None

    return {
        "running": running,
        "last_exit_code": last_exit,
        "log_tail": log_tail,
        "timeout_seconds": RUN_TIMEOUT,
        "uptime_seconds": round(time.time() - _start_time, 1),
        "progress": progress,
        "log_progress": log_progress,
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
        "max_crawl_subpages": Settings().max_crawl_subpages,
    }


PROMPTS_FILE = STATE_DIR / "prompts.json"


@app.get("/prompts")
def get_prompts():
    """Return crawl prompts (link triage + extraction). From saved file or defaults."""
    ensure_dirs()
    if PROMPTS_FILE.is_file():
        try:
            return json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return get_default_crawl_prompts(Settings())


class PromptsUpdate(BaseModel):
    link_triage_system: str | None = None
    link_triage_user: str | None = None
    extraction_system: str | None = None


@app.put("/prompts")
def update_prompts(body: PromptsUpdate):
    """Update one or more crawl prompts. Saved to state/prompts.json for next run."""
    ensure_dirs()
    current = get_prompts()
    if body.link_triage_system is not None:
        current["link_triage_system"] = body.link_triage_system
    if body.link_triage_user is not None:
        current["link_triage_user"] = body.link_triage_user
    if body.extraction_system is not None:
        current["extraction_system"] = body.extraction_system
    PROMPTS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return {"message": "Prompts saved.", "path": str(PROMPTS_FILE)}


def _file_list_with_mtime(dir_path: Path) -> list[dict]:
    files = []
    for f in dir_path.iterdir():
        if f.is_file():
            try:
                st = f.stat()
                mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                size_bytes = st.st_size
            except Exception:
                mtime_iso = None
                size_bytes = None
            entry = {"name": f.name, "mtime_iso": mtime_iso, "size_bytes": size_bytes}
            if size_bytes is not None:
                if size_bytes < 1024:
                    entry["size_human"] = f"{size_bytes} B"
                elif size_bytes < 1024 * 1024:
                    entry["size_human"] = f"{size_bytes / 1024:.1f} KB"
                else:
                    entry["size_human"] = f"{size_bytes / (1024 * 1024):.1f} MB"
            files.append(entry)
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
    """List state/checkpoint files in DATA_DIR/state with mtime and size."""
    ensure_dirs()
    return {"files": _file_list_with_mtime(STATE_DIR)}


@app.get("/state/{filename}")
def download_state(filename: str):
    """Download a state file by name (e.g. checkpoint JSON, run.log)."""
    path = STATE_DIR / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)


@app.delete("/state/{filename}")
def delete_state(filename: str):
    """Delete a state file (e.g. checkpoint JSON to reset a run). Blocked while a run is in progress."""
    with _run_lock:
        if _running:
            raise HTTPException(status_code=409, detail="Cannot delete state files while a run is in progress.")
    safe = Path(filename).name
    path = STATE_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    path.unlink()
    return {"message": f"Deleted {safe}"}


@app.delete("/output/{filename}")
def delete_output(filename: str):
    """Delete an output file."""
    safe = Path(filename).name
    path = OUTPUT_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    path.unlink()
    return {"message": f"Deleted {safe}"}
