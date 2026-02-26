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
from io import BytesIO
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from config import Settings
from website_enricher import get_default_crawl_prompts

# CSV format definitions for validation and pipeline (required / optional column names)
SUPPORTED_FORMATS = {
    "attio_people": {
        "description": "Attio People export — for enriching existing CRM records",
        "required": ["record id", "company > domains"],
        "optional": ["record", "job title", "email addresses", "phone numbers", "company"],
        "use_with_script": "enrich_justcall",
    },
    "attio_companies": {
        "description": "Attio Companies export — for dedup checking",
        "required": ["domains", "name"],
        "optional": ["description", "primary_location", "segment", "office_phone"],
        "use_with_script": "main",
    },
    "url_list": {
        "description": "URL list — one URL per line (.txt) or CSV with url/website/domain column",
        "required": ["url", "website", "domain"],  # at least one
        "optional": ["name", "firm", "company"],
        "use_with_script": "enrich_urls",
    },
    "campaign": {
        "description": "Campaign/JustCall contacts — for re-enriching campaign data",
        "required": ["person record id", "name", "website"],
        "optional": ["occupation", "email", "phone", "company"],
        "use_with_script": "enrich_justcall (with --format campaign)",
    },
    "enriched_people": {
        "description": "Enriched output (JustCall-style) — for campaign creation",
        "required": ["phone numbers", "first_name", "last_name"],
        "optional": ["company", "company > domains", "job title", "email addresses", "record id"],
        "use_with_script": "campaign",
    },
}
# Normalized column name sets for detection
ATTIO_PEOPLE_COLS = {"record id", "record", "job title", "company > domains", "company", "email addresses", "phone numbers"}
CAMPAIGN_COLS = {"person record id", "name", "occupation", "website", "email", "phone"}
URL_COLS = {"url", "website", "domain", "website_url", "domains"}
ENRICHED_PEOPLE_COLS = {"phone numbers", "first_name", "last_name", "company", "company > domains", "job title", "record id", "enrichment_status"}

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
LAST_RUN_SUMMARY_FILE = STATE_DIR / "last_run_summary.json"
CAMPAIGNS_FILE = STATE_DIR / "campaigns.json"
OUTPUT_METADATA_FILE = STATE_DIR / "output_metadata.json"
_last_run_params: dict = {}  # run_name, script for background thread to write summary

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


def _parse_log_observability() -> dict:
    """Parse run.log for current domain and issue counts. Returns { current_domain, issues }."""
    result = {"current_domain": None, "issues": []}
    log_path = STATE_DIR / "run.log"
    if not log_path.is_file():
        return result
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            m = re.search(r"Enriching:\s*[^\s(]+?\s*\(([^)]+)\)", line)
            if m:
                result["current_domain"] = m.group(1).strip()
                break
        counts = {"ERROR": 0, "WARNING": 0, "timeout": 0, "DNS": 0, "parked": 0}
        for line in lines:
            ll = line.lower()
            if "error" in ll:
                counts["ERROR"] += 1
            elif "warning" in ll:
                counts["WARNING"] += 1
            elif "timeout" in ll:
                counts["timeout"] += 1
            elif "dns" in ll or "does not resolve" in ll:
                counts["DNS"] += 1
            elif "parked" in ll:
                counts["parked"] += 1
        result["issues"] = [{"type": k, "count": v} for k, v in counts.items() if v > 0]
    except Exception:
        pass
    return result


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


def _enrichment_breakdown_from_checkpoint() -> dict | None:
    """Return counts: with_dms, no_dms, out_of_scope, failed, parked_dead from checkpoint."""
    for name in ("justcall_checkpoint.json", "url_enrichment_checkpoint.json", "checkpoint.json"):
        path = STATE_DIR / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            phase2 = data.get("phase2", {})
            enriched = phase2.get("enriched_urls", []) or []
            enrichments = phase2.get("enrichments", {})
            with_dms = no_dms = out_of_scope = no_data = 0
            for url in enriched:
                d = enrichments.get(url)
                if d is None:
                    no_data += 1
                elif d.get("out_of_scope"):
                    out_of_scope += 1
                elif d.get("decision_makers"):
                    with_dms += 1
                else:
                    no_dms += 1
            total = with_dms + no_dms + out_of_scope + no_data
            if total == 0:
                return None
            return {
                "with_dms": with_dms,
                "no_dms": no_dms,
                "out_of_scope": out_of_scope,
                "no_data": no_data,
                "total": total,
                "checkpoint_file": name,
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
            "run": "POST /run — body: {\"script\", \"input_file\"?, \"output_format\"?, \"csv_format\"?, \"concurrency\"?} (script: enrich_justcall | enrich_urls | main; csv_format for enrich_justcall: attio | campaign)",
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
            "formats": "GET /formats — supported CSV formats and required columns",
            "validate": "POST /validate — validate uploaded CSV (body: multipart file)",
            "pipeline_status": "GET /pipeline/status — CSV pipeline stages (needs_enrichment, campaign_ready, etc.)",
            "campaign_preview": "GET /campaign/preview?filename=… — preview output CSV for campaign",
            "campaign_create": "POST /campaign/create — create JustCall campaign from output CSV",
            "campaign_status": "GET /campaign/status/{id} — JustCall campaign details",
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
    global _pipeline_cache
    _pipeline_cache = None
    return {"filename": safe_name, "path": str(path)}


def _sanitize_run_name(s: str | None) -> str:
    """Safe filename fragment from run name."""
    if not s or not s.strip():
        return ""
    return re.sub(r"[^\w\-]", "_", s.strip())[:80]


def _unique_output_name(name: str) -> str:
    """Append a short suffix if the output file already exists to avoid overwriting."""
    if not (OUTPUT_DIR / name).exists():
        return name
    stem, ext = name.rsplit(".", 1) if "." in name else (name, "csv")
    return f"{stem}_{uuid.uuid4().hex[:6]}.{ext}"


class RunRequest(BaseModel):
    script: str  # "enrich_justcall" | "main" | "enrich_urls"
    input_file: str | None = None  # required for enrich_justcall and enrich_urls
    output_format: str | None = None  # for enrich_urls: "default" | "justcall"
    csv_format: str | None = None  # for enrich_justcall: "attio" | "campaign" (omit = auto-detect)
    concurrency: int | None = None  # optional; e.g. 4 for enrich_justcall / enrich_urls / main
    force_recrawl: str | None = None  # "all" | "no-dm" | None
    web_search_enabled: bool | None = None  # if set, overrides env for this run (Phase 2b fallback)
    run_name: str | None = None  # optional; used in output filename (e.g. NSW_CAANZ_Feb26)


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
    csv_format = body.csv_format
    concurrency = body.concurrency
    force_recrawl = body.force_recrawl
    out_name = None
    if force_recrawl and force_recrawl not in ("all", "no-dm"):
        raise HTTPException(status_code=400, detail="force_recrawl must be 'all', 'no-dm', or null")
    if csv_format is not None and csv_format not in ("attio", "campaign"):
        raise HTTPException(status_code=400, detail="csv_format must be 'attio', 'campaign', or null")
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
        run_name_safe = _sanitize_run_name(body.run_name)
        out_name = f"attio_people_enriched_{run_name_safe}.csv" if run_name_safe else f"attio_people_enriched_{uuid.uuid4().hex[:8]}.csv"
        out_name = _unique_output_name(out_name)
        output_path = OUTPUT_DIR / out_name
        checkpoint_path = STATE_DIR / "justcall_checkpoint.json"
        cmd = [
            "python", "-m", "enrich_justcall",
            "--input", str(input_path),
            "--output", str(output_path),
            "--checkpoint", str(checkpoint_path),
        ]
        if csv_format in ("attio", "campaign"):
            cmd.extend(["--format", csv_format])
        if concurrency is not None:
            cmd.extend(["--concurrency", str(concurrency)])
        if force_recrawl:
            cmd.extend(["--force-recrawl", force_recrawl])
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
            run_name_safe = _sanitize_run_name(body.run_name)
            out_name = f"url_enrichment_people_{run_name_safe}.csv" if run_name_safe else f"url_enrichment_people_{uuid.uuid4().hex[:8]}.csv"
            out_name = _unique_output_name(out_name)
            cmd.extend(["--justcall-output", str(OUTPUT_DIR / out_name)])
        if concurrency is not None:
            cmd.extend(["--concurrency", str(concurrency)])
        if force_recrawl:
            cmd.extend(["--force-recrawl", force_recrawl])
    else:
        # main.py: full directory pipeline (phase 1–3)
        checkpoint_path = STATE_DIR / "checkpoint.json"
        cmd = [
            "python", "-m", "main",
            "--checkpoint", str(checkpoint_path),
        ]
        if force_recrawl:
            cmd.extend(["--force-recrawl", force_recrawl])
        output_path = OUTPUT_DIR  # main.py writes companies.csv / people.csv there

    env = os.environ.copy()
    env["OUTPUT_DIR"] = str(OUTPUT_DIR)
    if concurrency is not None:
        env["MAX_CONCURRENT_CRAWLS"] = str(concurrency)
        env["DIRECTORY_MAX_CONCURRENT"] = str(concurrency)
    if body.web_search_enabled is not None:
        env["WEB_SEARCH_ENABLED"] = "true" if body.web_search_enabled else "false"

    global _running, _last_exit_code
    cwd = Path(__file__).resolve().parent
    log_path = STATE_DIR / "run.log"

    with _run_lock:
        if _running:
            raise HTTPException(status_code=409, detail="A run is already in progress. Check GET /run/status.")
        _running = True
        _last_exit_code = None

    _last_run_params["run_name"] = body.run_name
    _last_run_params["script"] = script
    _last_run_params["output_file_when_done"] = out_name if script in ("enrich_justcall", "enrich_urls") else None

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
            finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                LAST_RUN_FILE.write_text(
                    json.dumps({
                        "script": script,
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "exit_code": proc.returncode,
                    }),
                    encoding="utf-8",
                )
            except Exception:
                pass
            try:
                breakdown = _enrichment_breakdown_from_checkpoint()
                try:
                    t0 = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                    t1 = datetime.strptime(finished_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                    duration_seconds = max(0, int(t1 - t0))
                except Exception:
                    duration_seconds = 0
                summary = {
                    "script": script,
                    "run_name": _last_run_params.get("run_name"),
                    "output_file_when_done": _last_run_params.get("output_file_when_done"),
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_seconds": duration_seconds,
                    "exit_code": proc.returncode,
                    "total_processed": breakdown["total"] if breakdown else 0,
                    "with_dms": breakdown["with_dms"] if breakdown else 0,
                    "no_dms": breakdown["no_dms"] if breakdown else 0,
                    "out_of_scope": breakdown["out_of_scope"] if breakdown else 0,
                    "no_data": breakdown["no_data"] if breakdown else 0,
                }
                LAST_RUN_SUMMARY_FILE.write_text(json.dumps(summary, indent=2), encoding="utf-8")
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
    enrichment_breakdown = _enrichment_breakdown_from_checkpoint()
    log_obs = _parse_log_observability()
    last_run_summary = None
    if LAST_RUN_SUMMARY_FILE.exists():
        try:
            last_run_summary = json.loads(LAST_RUN_SUMMARY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "running": running,
        "last_exit_code": last_exit,
        "log_tail": log_tail,
        "timeout_seconds": RUN_TIMEOUT,
        "uptime_seconds": round(time.time() - _start_time, 1),
        "progress": progress,
        "log_progress": log_progress,
        "last_run": last_run,
        "last_run_summary": last_run_summary,
        "enrichment_breakdown": enrichment_breakdown,
        "current_domain": log_obs["current_domain"],
        "issues": log_obs["issues"],
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


@app.get("/formats")
def list_formats():
    """Return supported CSV formats with required/optional columns and which script to use."""
    return {"formats": SUPPORTED_FORMATS}


def _normalize_col(s: str) -> str:
    return (s or "").strip().lower()


def _detect_csv_format(cols_lower: set[str]) -> str:
    if cols_lower >= ATTIO_PEOPLE_COLS or ("record id" in cols_lower and "company > domains" in cols_lower):
        return "attio_people"
    if CAMPAIGN_COLS <= cols_lower or ("person record id" in cols_lower and "website" in cols_lower):
        return "campaign"
    if cols_lower & set(URL_COLS) and ("url" in cols_lower or "website" in cols_lower or "domain" in cols_lower):
        return "url_list"
    if "domains" in cols_lower and "name" in cols_lower:
        return "attio_companies"
    if ENRICHED_PEOPLE_COLS <= cols_lower or ("phone numbers" in cols_lower and "first_name" in cols_lower):
        return "enriched_people"
    return "unknown"


class CSVValidationResult(BaseModel):
    valid: bool
    format_detected: str
    columns_found: list[str]
    columns_missing: list[str]
    columns_extra: list[str]
    row_count: int
    sample_rows: list[dict]
    warnings: list[str]


@app.post("/validate", response_model=CSVValidationResult)
async def validate_csv(file: UploadFile = File(...)):
    """Validate a CSV against supported formats; return detected format, missing/extra columns, sample rows."""
    ensure_dirs()
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload a CSV file")
    try:
        content = await file.read()
        df = pd.read_csv(BytesIO(content), nrows=500)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse CSV: {e}")
    cols = list(df.columns)
    cols_lower_set = {_normalize_col(c) for c in cols}
    cols_lower_list = [_normalize_col(c) for c in cols]
    format_detected = _detect_csv_format(cols_lower_set)
    info = SUPPORTED_FORMATS.get(format_detected, {})
    required = info.get("required", [])
    optional = info.get("optional", [])
    allowed = set(required) | set(optional)
    if format_detected == "url_list":
        required_any = set(required)
        missing = [] if (required_any & cols_lower_set) else list(required_any)[:1]
    else:
        missing = [r for r in required if r not in cols_lower_set]
    extra = [c for c in cols_lower_list if c not in allowed and format_detected != "unknown"]
    valid = len(missing) == 0
    warnings = []
    if extra:
        warnings.append(f"Unexpected columns: {', '.join(extra[:10])}")
    if format_detected == "unknown":
        warnings.append("Format not recognized; use one of the supported formats from GET /formats")
    sample = df.head(3).fillna("").astype(str).to_dict(orient="records")
    return CSVValidationResult(
        valid=valid,
        format_detected=format_detected,
        columns_found=cols,
        columns_missing=missing,
        columns_extra=extra[:15],
        row_count=len(df),
        sample_rows=sample,
        warnings=warnings,
    )


_pipeline_cache: dict | None = None
_pipeline_cache_time: float = 0
PIPELINE_CACHE_TTL = 30.0


def _get_pipeline_status() -> dict:
    """Classify CSVs by enrichment stage. Cached for PIPELINE_CACHE_TTL seconds."""
    global _pipeline_cache, _pipeline_cache_time
    now = time.time()
    if _pipeline_cache is not None and (now - _pipeline_cache_time) < PIPELINE_CACHE_TTL:
        return _pipeline_cache
    ensure_dirs()
    stages = {
        "needs_enrichment": [],
        "in_progress": [],
        "partially_enriched": [],
        "fully_enriched": [],
        "campaign_ready": [],
    }
    input_files = {f.name for f in INPUT_DIR.iterdir() if f.is_file() and f.suffix.lower() in (".csv", ".txt")}
    output_files = list(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.exists() else []
    for f in output_files:
        if not f.is_file() or f.suffix.lower() != ".csv":
            continue
        try:
            df = pd.read_csv(f, nrows=1000)
            row_count = len(df)
            cols_lower = {_normalize_col(c) for c in df.columns}
            has_status = "enrichment_status" in cols_lower
            has_phone = "phone numbers" in cols_lower or "phone_numbers" in cols_lower
            with_phone = 0
            if has_phone:
                phone_col = "phone numbers" if "phone numbers" in df.columns else "phone_numbers"
                with_phone = df[phone_col].fillna("").astype(str).str.strip().str.match(r"^\+?\d").sum()
            if has_status:
                status_counts = df["enrichment_status"].fillna("").astype(str)
                enriched = status_counts.str.startswith("enriched").sum()
                with_dms = (status_counts == "enriched_with_dms").sum()
                if enriched == row_count and row_count > 0:
                    stages["fully_enriched"].append({
                        "name": f.name,
                        "row_count": row_count,
                        "with_dms": int(with_dms),
                        "with_phone": int(with_phone) if has_phone else None,
                        "mtime_iso": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    })
                elif enriched > 0:
                    stages["partially_enriched"].append({
                        "name": f.name,
                        "row_count": row_count,
                        "enriched": int(enriched),
                        "with_dms": int(with_dms),
                        "with_phone": int(with_phone) if has_phone else None,
                        "mtime_iso": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    })
                else:
                    stages["needs_enrichment"].append({
                        "name": f.name,
                        "row_count": row_count,
                        "mtime_iso": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "is_input": False,
                    })
            else:
                stages["fully_enriched"].append({
                    "name": f.name,
                    "row_count": row_count,
                    "with_phone": int(with_phone) if has_phone else None,
                    "mtime_iso": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            if has_phone and with_phone >= 1 and "first_name" in cols_lower:
                stages["campaign_ready"].append({
                    "name": f.name,
                    "row_count": row_count,
                    "with_phone": int(with_phone),
                    "mtime_iso": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
        except Exception:
            stages["partially_enriched"].append({"name": f.name, "row_count": 0, "error": "could not read"})
    input_meta = {f["name"]: f for f in _file_list_with_mtime(INPUT_DIR)}
    for name in input_files:
        entry = {"name": name, **{k: input_meta.get(name, {}).get(k) for k in ("mtime_iso", "size_bytes")}}
        if name.endswith(".txt"):
            entry["format_detected"] = "url_list"
            entry["suggested_script"] = "enrich_urls"
            entry["is_input"] = True
            stages["needs_enrichment"].append(entry)
            continue
        try:
            df_in = pd.read_csv(INPUT_DIR / name, nrows=5)
            cols_lower_set = {_normalize_col(c) for c in df_in.columns}
            fmt = _detect_csv_format(cols_lower_set)
            entry["format_detected"] = fmt
            entry["suggested_script"] = (
                "enrich_justcall" if fmt in ("attio_people", "campaign") else
                "enrich_urls" if fmt == "url_list" else "main" if fmt == "attio_companies" else None
            )
        except Exception:
            entry["format_detected"] = "unknown"
            entry["suggested_script"] = None
        entry["is_input"] = True
        out_match = any(o.get("name") == name for o in stages["fully_enriched"] + stages["partially_enriched"])
        if not out_match:
            stages["needs_enrichment"].append(entry)
    _pipeline_cache = stages
    _pipeline_cache_time = now
    return stages


@app.get("/pipeline/status")
def pipeline_status():
    """Classify CSVs by enrichment stage: needs_enrichment, in_progress, partially_enriched, fully_enriched, campaign_ready."""
    return _get_pipeline_status()


def _append_campaign_registry(
    campaign_id: str, campaign_name: str, csv_filename: str,
    contacts_added: int, lead_grades: dict | None = None,
) -> None:
    ensure_dirs()
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "csv_filename": csv_filename,
        "created_at": created_at,
        "contacts_added": contacts_added,
        "lead_grades": lead_grades or {},
    }
    try:
        if CAMPAIGNS_FILE.is_file():
            data = json.loads(CAMPAIGNS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                data = []
        else:
            data = []
        data.append(entry)
        CAMPAIGNS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


@app.get("/campaigns")
def list_campaigns():
    """List created campaigns (newest first) from registry."""
    ensure_dirs()
    if not CAMPAIGNS_FILE.is_file():
        return {"campaigns": []}
    try:
        data = json.loads(CAMPAIGNS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return {"campaigns": []}
        return {"campaigns": list(reversed(data))}
    except Exception:
        return {"campaigns": []}


@app.get("/campaign/preview")
def campaign_preview(filename: str):
    """Preview an output CSV for campaign creation: row count, with_phone count, lead grades."""
    from justcall_api import grade_lead

    path = OUTPUT_DIR / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read CSV: {e}")
    cols_lower = {_normalize_col(c): c for c in df.columns}
    phone_col = cols_lower.get("phone numbers") or cols_lower.get("phone_numbers")
    first_col = cols_lower.get("first_name")
    with_phone = 0
    if phone_col:
        with_phone = int(df[phone_col].fillna("").astype(str).str.strip().str.match(r"^\+?\d").sum())

    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    for _, row in df.iterrows():
        g = grade_lead(row.to_dict(), cols_lower)
        grade_counts[g] = grade_counts.get(g, 0) + 1

    return {
        "filename": path.name,
        "row_count": len(df),
        "with_phone": with_phone,
        "has_first_name": first_col is not None,
        "columns": list(df.columns),
        "lead_grades": grade_counts,
    }


CAMPAIGN_NAME_RE = re.compile(r"^[A-Z0-9][A-Za-z0-9_\- ]{2,80}$")

CAMPAIGN_SCHEMA = {
    "required_columns": ["phone numbers", "first_name"],
    "recommended_columns": ["last_name", "company", "email addresses", "job title"],
    "custom_field_columns": ["record id", "job title", "company > domains", "linkedin", "description"],
}


class CampaignCreateRequest(BaseModel):
    csv_file: str
    campaign_name: str
    dial_mode: str = "Autodial"  # Autodial, Predictive, Dynamic
    country_code: str = "AU"
    min_grade: str = "D"  # only import leads >= this grade (A, B, C, D)


@app.get("/campaign/schema")
def campaign_schema():
    """Return the standardized campaign data schema and naming rules."""
    return {
        "schema": CAMPAIGN_SCHEMA,
        "name_pattern": "^[A-Z0-9][A-Za-z0-9_- ]{2,80}$",
        "name_example": "OBC_AU_ACC_NSW_Feb26",
        "grades": {"A": "phone+name+title+email", "B": "phone+name+(title|email)", "C": "phone+name", "D": "phone only"},
        "dial_modes": ["Autodial", "Predictive", "Dynamic"],
    }


@app.post("/campaign/create")
async def create_campaign(body: CampaignCreateRequest):
    """Create a JustCall campaign from an enriched output CSV with lead grading and custom fields."""
    try:
        from justcall_api import JustCallClient, build_justcall_contact, grade_lead
    except ImportError:
        raise HTTPException(status_code=503, detail="JustCall client not available")

    if not CAMPAIGN_NAME_RE.match(body.campaign_name):
        raise HTTPException(
            status_code=400,
            detail="Campaign name must start with uppercase/digit, 3-81 chars, letters/digits/underscore/dash/space only. Example: OBC_AU_ACC_NSW_Feb26",
        )
    if body.min_grade not in ("A", "B", "C", "D"):
        raise HTTPException(status_code=400, detail="min_grade must be A, B, C, or D")

    client = JustCallClient()
    if not client.is_configured():
        raise HTTPException(status_code=400, detail="JustCall API key and secret not configured. Set JUSTCALL_API_KEY and JUSTCALL_API_SECRET in .env")

    path = OUTPUT_DIR / Path(body.csv_file).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Output file not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read CSV: {e}")

    cols_lower = {_normalize_col(c): c for c in df.columns}
    phone_col = cols_lower.get("phone numbers") or cols_lower.get("phone_numbers")
    first_col = cols_lower.get("first_name")
    if not phone_col or not first_col:
        missing = []
        if not phone_col:
            missing.append("Phone numbers")
        if not first_col:
            missing.append("first_name")
        raise HTTPException(status_code=400, detail=f"CSV missing required columns: {', '.join(missing)}. See GET /campaign/schema")

    grade_order = "ABCD"
    min_idx = grade_order.index(body.min_grade)
    allowed_grades = set(grade_order[: min_idx + 1])

    contacts = []
    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    skipped = 0
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        g = grade_lead(row_dict, cols_lower)
        grade_counts[g] = grade_counts.get(g, 0) + 1
        if g not in allowed_grades:
            skipped += 1
            continue
        contact = build_justcall_contact(row_dict, cols_lower)
        if contact:
            contacts.append(contact)

    if not contacts:
        raise HTTPException(status_code=400, detail=f"No contacts with grade >= {body.min_grade} and valid phone. Grade distribution: {grade_counts}")

    try:
        campaign = await client.create_campaign_async(
            name=body.campaign_name,
            campaign_type=body.dial_mode,
            country_code=body.country_code,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"JustCall create campaign failed: {e}")

    cid = campaign.get("id") or campaign.get("campaign_id") or campaign.get("data", {}).get("id")
    if not cid:
        raise HTTPException(status_code=502, detail="JustCall did not return campaign id")
    cid_str = str(cid)

    try:
        await client.bulk_import_contacts_async(cid_str, contacts)
    except Exception as e:
        _append_campaign_registry(cid_str, body.campaign_name, body.csv_file, 0, grade_counts)
        return {
            "campaign_id": cid_str,
            "campaign_name": body.campaign_name,
            "created": True,
            "contacts_added": 0,
            "lead_grades": grade_counts,
            "error": str(e),
            "message": "Campaign created but adding contacts failed. Add contacts manually or retry.",
        }

    _append_campaign_registry(cid_str, body.campaign_name, body.csv_file, len(contacts), grade_counts)
    return {
        "campaign_id": cid_str,
        "campaign_name": body.campaign_name,
        "created": True,
        "contacts_added": len(contacts),
        "skipped_below_grade": skipped,
        "min_grade": body.min_grade,
        "lead_grades": grade_counts,
    }


@app.get("/campaign/status/{campaign_id}")
def campaign_status(campaign_id: str):
    """Get JustCall campaign details (if API supports it)."""
    try:
        from justcall_api import JustCallClient
    except ImportError:
        raise HTTPException(status_code=503, detail="JustCall client not available")
    client = JustCallClient()
    if not client.is_configured():
        raise HTTPException(status_code=400, detail="JustCall not configured")
    try:
        data = client.get_campaign(campaign_id)
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/checkpoint/stats")
def checkpoint_stats():
    """Summary of checkpoint state: counts of enriched, with DMs, no DMs, out of scope."""
    ensure_dirs()
    totals = {"enriched": 0, "with_dms": 0, "no_dms": 0, "out_of_scope": 0, "no_data": 0}
    for name in ("justcall_checkpoint.json", "url_enrichment_checkpoint.json", "checkpoint.json"):
        path = STATE_DIR / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            phase2 = data.get("phase2", {})
            enriched = phase2.get("enriched_urls", [])
            enrichments = phase2.get("enrichments", {})
            totals["enriched"] += len(enriched)
            for url in enriched:
                d = enrichments.get(url)
                if d is None:
                    totals["no_data"] += 1
                elif d.get("out_of_scope"):
                    totals["out_of_scope"] += 1
                elif d.get("decision_makers"):
                    totals["with_dms"] += 1
                else:
                    totals["no_dms"] += 1
            return {"checkpoint_file": name, **totals}
        except Exception:
            pass
    return totals


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


def _load_output_metadata() -> dict:
    """Load output_metadata.json: filename -> { tags: [], run_name: "" }."""
    ensure_dirs()
    if not OUTPUT_METADATA_FILE.is_file():
        return {}
    try:
        data = json.loads(OUTPUT_METADATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_output_metadata(data: dict) -> None:
    OUTPUT_METADATA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


@app.get("/output/metadata")
def get_output_metadata():
    """Get tags and run_name for all output files."""
    return {"metadata": _load_output_metadata()}


class OutputMetadataUpdate(BaseModel):
    filename: str
    tags: list[str] | None = None
    run_name: str | None = None


@app.put("/output/metadata")
def update_output_metadata(body: OutputMetadataUpdate):
    """Set tags and/or run_name for one output file."""
    ensure_dirs()
    meta = _load_output_metadata()
    key = Path(body.filename).name
    if key not in meta:
        meta[key] = {"tags": [], "run_name": ""}
    if body.tags is not None:
        meta[key]["tags"] = body.tags
    if body.run_name is not None:
        meta[key]["run_name"] = body.run_name
    _save_output_metadata(meta)
    return {"metadata": meta}


@app.get("/output/summary")
def output_summary(filename: str, format: str = "json"):
    """Return run summary for an output CSV: row count, with_phone, status counts. format=json (default) or csv for download."""
    path = OUTPUT_DIR / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read CSV: {e}")
    cols_lower = {_normalize_col(c): c for c in df.columns}
    phone_col = cols_lower.get("phone numbers") or cols_lower.get("phone_numbers")
    status_col = cols_lower.get("enrichment_status")
    domain_col = cols_lower.get("company > domains") or cols_lower.get("company_domain") or cols_lower.get("domains")
    row_count = len(df)
    with_phone = 0
    if phone_col:
        with_phone = int(df[phone_col].fillna("").astype(str).str.strip().str.match(r"^\+?\d").sum())
    status_counts = {}
    if status_col and status_col in df.columns:
        status_counts = df[status_col].fillna("").astype(str).value_counts().to_dict()
    domain_list = []
    if domain_col and domain_col in df.columns and row_count <= 5000:
        for _, row in df.iterrows():
            dom = str(row.get(domain_col, "") or "").strip()
            st = str(row.get(status_col, "") or "").strip() if status_col else ""
            if dom:
                domain_list.append({"domain": dom, "enrichment_status": st})
    summary = {
        "filename": path.name,
        "row_count": row_count,
        "with_phone": with_phone,
        "status_counts": status_counts,
        "domains": domain_list[:500],
    }
    if format == "csv":
        from fastapi.responses import StreamingResponse
        from io import StringIO
        if domain_list:
            import csv
            buf = StringIO()
            writer = csv.DictWriter(buf, fieldnames=["domain", "enrichment_status"])
            writer.writeheader()
            writer.writerows(domain_list)
            content = buf.getvalue().encode("utf-8")
        else:
            content = f"row_count,with_phone\n{row_count},{with_phone}\n".encode("utf-8")
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{path.stem}_summary.csv"'},
        )
    return summary


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
    global _pipeline_cache
    safe = Path(filename).name
    path = OUTPUT_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    path.unlink()
    _pipeline_cache = None
    return {"message": f"Deleted {safe}"}
