# Scraping Accountantlist

Firm website enricher: crawls accounting-firm sites, uses an LLM to extract company and decision-maker data, and outputs Attio CRM–ready CSVs. Supports three workflows: **generic URL list**, **Attio People backfill**, and **full directory pipeline** (accountantlist.com.au harvest → enrich → dedup).

This README is aimed at bringing a team member up to speed quickly, including if they last saw an earlier version of the tool.

---

## Table of contents

- [Quick orientation](#quick-orientation)
- [Config](#config)
- [Railway + dashboard](#railway--dashboard)
- [Scraper structure](#scraper-structure)
- [Entry points and when to use which](#entry-points-and-when-to-use-which)
- [What’s changed from earlier versions](#whats-changed-from-earlier-versions)
- [Local development](#local-development)
- [Further detail](#further-detail)

---

## Quick orientation

| You want to… | Use |
|--------------|-----|
| Enrich a list of firm URLs (TXT or CSV) | **`enrich_urls.py`** |
| Backfill poor Attio People records by crawling company sites | **`enrich_justcall.py`** |
| Run the full flow: scrape accountantlist.com.au → enrich → dedup → export | **`main.py`** |

All three share the same **enrichment engine** (`website_enricher.py`): browser-based crawl, link triage (LLM or keyword), LLM extraction via OpenRouter (Grok), web-search fallback, checkpointing. The engine is configured via **`config.py`** and **environment variables** (see [Config](#config)).

---

## Config

- **Source of truth:** `scraper/config.py` (Pydantic Settings). All options have defaults and validation (e.g. timeouts, concurrency bounds).
- **Local overrides:** `scraper/.env` (not in repo). Copy from `scraper/.env.example` and set keys.
- **Railway:** No `.env` in the repo; set variables in the **Railway dashboard** (or CLI). They override defaults the same way as `.env`.

### Key variables

| Variable | Required | Default | Notes |
|----------|----------|--------|--------|
| `OPENROUTER_API_KEY` | Yes (for enrichment) | — | From [OpenRouter](https://openrouter.ai) |
| `OPENROUTER_MODEL` | No | `x-ai/grok-4.1-fast` | LLM for extraction + link triage |
| `ATTIO_API_KEY` | For dedup / Attio flows | — | Attio → Settings → API |
| `WEB_SEARCH_ENABLED` | No | `true` | Fallback when crawl finds no DMs |
| `WEB_SEARCH_MODEL` | No | `x-ai/grok-4.1-fast:online` | Model with search plugin |
| `LLM_LINK_TRIAGE` | No | `true` | Use LLM to pick sub-pages to crawl |
| `PAGE_TIMEOUT` | No | `45000` | Page load timeout (ms); hard cap is +10s |
| `MAX_CONCURRENT_CRAWLS` | No | `5` | Concurrent browser crawls |
| `MAX_CRAWL_SUBPAGES` | No | `10` | Max sub-pages per site (LLM picks up to this) |
| `OUTPUT_DIR` | No | `data/output` | Where CSVs are written (local) |
| `DATA_DIR` | Railway + volume | — | e.g. `/data`; then input/output/state live under it |
| `SCRAPER_RUN_TIMEOUT_SECONDS` | No | `3600` | Max run time for API-triggered jobs (Railway) |

Full list and ranges are in `scraper/config.py`.

---

## Railway + dashboard

The app is deployed as a **single service** that runs the **API server** (`server.py`). The server accepts uploads, triggers scraper runs (enrich_justcall, enrich_urls, or main), and serves a **web dashboard** for upload / run / logs / download.

### Build and run

- **Recommended:** Use the **Dockerfile at repo root**. In Railway: **Service → Settings → Build → Builder = Dockerfile**. It builds the `scraper/` app and installs Playwright/Chromium; no need to set Root Directory.
- **Alternative:** Set **Root Directory** to `scraper` and use Railpack (ensure `requirements.txt` and Python are detected). If you see “Error creating build plan with Railpack”, switch to the root Dockerfile.
- **Start command** is set in `railway.json`: `uvicorn server:app --host 0.0.0.0 --port $PORT`. Health check: `/health`.

### Persistence and directories

- **Volume (recommended):** Add a volume, mount path **`/data`**. Set **`DATA_DIR=/data`** in Variables. Then:
  - **Input:** `/data/input` (uploaded files)
  - **Output:** `/data/output` (result CSVs)
  - **State:** `/data/state` (checkpoints, run.log, prompts)
- Without a volume, `DATA_DIR` defaults to `data/` relative to the app; data does not persist across deploys.

### Exposing the app

- **Settings → Networking → Generate domain** (e.g. `your-app.up.railway.app`).

### Dashboard (web UI)

- **URL:** `https://your-app.up.railway.app/dashboard`
- **Use it to:** upload CSV/TXT, choose script (enrich_justcall / enrich_urls / main), pick input file and options (e.g. output format, CSV format, concurrency), start a run, watch live log tail, see progress/ETA, list and download output/state files.
- Share the dashboard link so others can run jobs without using curl.

### API (summary)

- **POST /upload** — Upload a file (stored under `DATA_DIR/input/`).
- **POST /run** — Start a run. Body: `script` (`enrich_justcall` \| `enrich_urls` \| `main`), optional `input_file`, `output_format` (enrich_urls: `default` \| `justcall`), `csv_format` (enrich_justcall: `attio` \| `campaign`), `concurrency`, `force_recrawl` (`all` \| `no-dm`). Returns immediately; run continues in background.
- **GET /run/status** — Running?, log tail, timeout, progress (from checkpoint/log), last run info. Use this to monitor long runs.
- **POST /run/cancel** — Cancel current run.
- **GET /config** — Effective config (data_dir, timeout_seconds, etc.).
- **GET /output**, **GET /input**, **GET /state** — List files with mtime/size.
- **GET /output/{filename}**, **GET /state/{filename}** — Download. **DELETE /output/{filename}**, **DELETE /state/{filename}** — Delete (state blocked while a run is in progress).
- **GET /checkpoint/stats** — Counts: enriched, with_dms, no_dms, out_of_scope, no_data.
- **GET /prompts**, **PUT /prompts** — Read/update crawl prompts (link triage + extraction) stored in state; used on next run.

Only one run can be active at a time; starting another returns **409** until the current one finishes or is cancelled. Runs are limited by **`SCRAPER_RUN_TIMEOUT_SECONDS`** (default 1 hour).

---

## Scraper structure

```
Scraping_Accountantlist/
├── README.md                 # This file
├── Dockerfile                # Root Dockerfile for Railway (builds scraper/)
├── railway.json              # Railway deploy/healthcheck (root)
└── scraper/
    ├── config.py             # All settings (env + validation)
    ├── models.py             # Pydantic models + LLM JSON schemas
    ├── website_enricher.py   # Core engine: crawl, triage, LLM, web search
    ├── checkpoint.py         # JSON checkpointing (resume)
    ├── attio_dedup.py        # Attio API lookups (dedup)
    ├── phone_utils.py        # E.164 normalisation
    ├── exporter.py           # Company/people records → CSVs (main.py)
    ├── segment_mapper.py     # Accountancy areas → Attio segments (main.py)
    ├── directory_scraper.py  # Phase 1: accountantlist.com.au (httpx + BeautifulSoup)
    ├── enrich_urls.py        # Entry: URL list → enrich → CSVs
    ├── enrich_justcall.py    # Entry: Attio People CSV → enrich → People CSV
    ├── main.py               # Entry: directory → Phase 1 → 2 → 3
    ├── server.py             # FastAPI: upload, run, status, dashboard, output/state
    ├── static/
    │   └── dashboard.html    # Dashboard UI
    ├── data/
    │   ├── input/            # Uploaded files (when using API)
    │   ├── output/           # Generated CSVs
    │   └── state/            # Checkpoints, run.log, prompts.json
    ├── docs/
    │   └── TEAM_ABOUT_PRIORITIZATION_PLAN.md
    ├── .env.example
    ├── requirements.txt
    ├── railway.json
    ├── Dockerfile            # Alternative: use from scraper/ as root in Railway
    └── SCRAPER_GUIDE.md      # Deeper technical guide (flows, CLI, output columns)
```

- **Engine:** `website_enricher.py` — browser pool, homepage + sub-page crawl, link triage (LLM or keyword), LLM extraction, web-search fallback, checkpoint write per firm.
- **Schemas:** `models.py` defines `LLMEnrichmentResponse` / `LLMWebSearchResponse` and thus what the LLM returns; change field descriptions here to change extraction behaviour.
- **Checkpoints:** Each script uses its own checkpoint file (see [Entry points](#entry-points-and-when-to-use-which)); state lives under `data/state/` (or `DATA_DIR/state` on Railway).

---

## Entry points and when to use which

| Script | Input | Output | Typical use |
|--------|--------|--------|-------------|
| **enrich_urls** | TXT (one URL per line) or CSV with url/website/domain column | Companies + people CSVs, or single “justcall”-style People CSV | Any list of firm URLs; campaign-style URL lists |
| **enrich_justcall** | Attio People CSV (or campaign/JustCall CSV with Record ID, Name, Website) | Single Attio People CSV (matched on Record ID) | Backfilling existing Attio People that have placeholder names / generic titles |
| **main** | None (reads directory) | companies.csv, people.csv (and checkpoint) | Full pipeline: scrape directory → enrich → dedup → export |

### Output format options

- **enrich_urls**
  - **`--output-format default`** → `companies.csv` + `people.csv`.
  - **`--output-format justcall`** → one CSV with Attio People–style columns (Record ID, Company, Domains, phones, emails, first/last name, Job title, etc.), suitable for Attio People import or JustCall-style workflows.
- **enrich_justcall**
  - **`--format attio`** / **`campaign`** — Use when auto-detect is wrong. Attio = standard Attio export columns; campaign = Person Record ID, Name, Occupation, Website, etc.

### Force recrawl (all entry points)

- **`--force-recrawl all`** — Ignore checkpoint; re-crawl every URL/site.
- **`--force-recrawl no-dm`** — Re-crawl only sites that previously had no decision makers (retry with web search / different pages).

Available in CLI and in the API/dashboard via the `force_recrawl` body parameter.

### Concurrency

- **enrich_urls / enrich_justcall:** `--concurrency N` (default 4). Also settable via API/dashboard.
- **main:** Uses `DIRECTORY_MAX_CONCURRENT` and `MAX_CONCURRENT_CRAWLS` from config; API can pass `concurrency` in the run body to override.

---

## What’s changed from earlier versions

If you’re coming back after an older version, here’s what to expect:

- **Single deployable app:** One Railway service runs **`server.py`** (FastAPI). No separate “scraper only” deploy; you upload input and trigger runs via API or dashboard.
- **Dashboard:** A **web UI** at **`/dashboard`** for upload, script selection, options (output format, CSV format, concurrency, force_recrawl), live logs, progress/ETA, and download of output/state. No need to curl unless you prefer it.
- **Three scripts from API:** **enrich_justcall**, **enrich_urls**, and **main** are all triggerable via **POST /run** with the right `script` and `input_file` (for enrich_justcall and enrich_urls).
- **enrich_urls** can output **justcall-style** single CSV (`output_format=justcall`) as well as default companies + people.
- **Prompts:** Link-triage and extraction prompts can be **read/updated via GET/PUT /prompts**; they’re stored in state and used on the next run (no code change needed for prompt tweaks).
- **Checkpoint stats:** **GET /checkpoint/stats** gives counts (enriched, with_dms, no_dms, out_of_scope, no_data) for observability.
- **State endpoints:** **GET /state** and **GET /state/{filename}** (and **DELETE /state/{filename}**) for checkpoint and run.log; **DELETE** is blocked while a run is in progress.
- **Run timeout:** Long runs are capped by **`SCRAPER_RUN_TIMEOUT_SECONDS`** (default 3600). Increase in Railway Variables if needed.
- **Dockerfile at repo root:** Railway can use the root **Dockerfile** to build the scraper (recommended) so Railpack detection issues are avoided.
- **Team/about prioritization:** Engine guarantees at least one “team” page when present and strengthens the link-triage prompt (see `scraper/docs/TEAM_ABOUT_PRIORITIZATION_PLAN.md`).

---

## Local development

```bash
cd scraper
python -m venv venv
source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt
playwright install chromium
cp .env.example .env       # then edit .env with your keys
```

- Run a script:  
  `python -m enrich_urls --input urls.txt --limit 5`  
  or  
  `python -m enrich_justcall --input attio_export.csv --limit 5`  
  or  
  `python -m main --phase 1`
- Run the API (and dashboard) locally:  
  `uvicorn server:app --reload --host 0.0.0.0 --port 8000`  
  Then open `http://localhost:8000/dashboard`.

---

## Further detail

- **Full CLI options, output column definitions, architecture diagram, troubleshooting:** see **`scraper/SCRAPER_GUIDE.md`**.
- **Team/about page prioritization design:** **`scraper/docs/TEAM_ABOUT_PRIORITIZATION_PLAN.md`**.
